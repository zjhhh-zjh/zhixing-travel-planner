"""对话服务层：编排层，产出统一事件流
框架化对应关系：
- 路由阶段 → chat_route（with_structured_output，替代手写 JSON 解析）
- 持久化   → Message.extra_info 存储富字段（original/inference_steps，前端历史渲染依赖）
"""
import asyncio
import re
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import (
    make_event, ROLE_SKILLS, ROLE_DONE, ROLE_ERROR, ROLE_ITINERARY,
    ROLE_ASSISTANT,
    ROLE_SKILL_UI,
)
from app.core.event_stream import EventStream
from app.core.intent import detect_intent
from app.agents.routers.chat_router import chat_route, RouterResult
from app.services.chat_stream_executor import execute_turn, try_direct_ticket_query
from app.services.itinerary_plan_service import generate_plan
from app.services.chat_compress_service import compress_content, COMPRESS_THRESHOLD
from app.services.conversation_service import get_owned_conversation
from app.models.message import Message
from app.utils.logger import app_logger
# 行程特征守卫：匹配行程天数标记，兼容中文/阿拉伯数字、Day格式
_DAY_MARK = re.compile(r"第\s*(\d+|[一二三四五六七八九十]+)\s*天|Day\s*\d", re.IGNORECASE)

# 清单生成步骤白名单：仅在行程生成及后续步骤才渲染清单
_ITINERARY_READY_STEPS = {"itinerary_generation", "budget_summarization", "report_generation"}


def _hit_auto_confirm(content: str) -> bool:
    """判断表单是否包含自动确认表述"""
    return detect_intent(content).auto_confirm


# 多目的地表单解析常量
_HEAD_DEST_PREFIX = "我想去"
_HEAD_DEST_SUFFIX = "旅行"
_HEAD_DAYS_LABEL = "旅行天数"

class _SkillUiRecorder:
    """ 卡片帧回放记录器——旁路记录本轮推送的 skill_ui 帧

    卡片此前只活在本轮 SSE/WS 事件里，刷新页面或切会话即消失。此处只做记录（不改推送行为），
    回合末把最近一条卡片帧写进 assistant 消息的 extra_info["skill_ui"]，历史接口随之透出，
    前端挂回 ChatMessage.skillUi 即可原样重渲染卡片
    """

    def __init__(self, inner: EventStream) -> None:
        self._inner = inner
        self.skill_ui: dict | None = None  # 最近一条卡片帧内容 {skill_name, data}

    def push(self, event: dict) -> None:
        try:
            if event.get("role") == ROLE_SKILL_UI and isinstance(event.get("content"), dict):
                # ★ 只记录、不改推送：最近一条 skill_ui 卡片帧留给回合末落库
                self.skill_ui = event["content"]
        except Exception:
            pass
        self._inner.push(event)

    def close(self) -> None:
        self._inner.close()

    # ★ 鸭子类型：push / close / async for 与 EventStream 完全一致，下游无感知
    def __aiter__(self):
        return self._inner.__aiter__()

async def mark_skill_ui_submitted(
    db: AsyncSession, conversation_id, skill_name: str, payload,
) -> None:
    """ 把「已提交 + 各段选中」写回卡片帧所在消息的 extra_info

    卡片帧此前已随消息落库，但"提交态"只活在前端组件里——切会话/刷新后
    卡片按历史帧重渲染时又回到未选可点态（用户实测：切回来还能点）。
    此处按 skill_name 找到最近一条带同款帧的 assistant 消息，把选中账本并入
    extra_info["skill_ui"]["submitted"]，历史接口随 extra_info 透出，前端据此直接
    渲染为已提交锁定态。仅记录展示态，异常只记日志、不影响提交流程。
    """
    try:
        raw = payload.model_dump() if hasattr(payload, "model_dump") else dict(payload)
        selections: dict[str, str] = {}
        for seg in raw.get("segments") or []:
            sid = seg.get("segment_id")
            picked = seg.get("option_id") or seg.get("hotel_id") or seg.get("brand_id")
            if sid and picked:
                selections[str(sid)] = str(picked)
        rows = (await db.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id, Message.role == "assistant")
            .order_by(Message.created_at.desc())
            .limit(10)
        )).scalars().all()
        for msg in rows:
            extra = dict(msg.extra_info or {})
            frame = extra.get("skill_ui")
            if isinstance(frame, dict) and frame.get("skill_name") == skill_name:
                extra["skill_ui"] = {**frame, "submitted": {"selections": selections}}
                msg.extra_info = extra  # ★ 重新赋值整列，SQLAlchemy 才会标记 JSON 变更为脏
                await db.commit()
                return
    except Exception:
        app_logger.exception("卡片提交态落库失败（仅影响历史回放，不影响主流程）")

def _manual_multi_city_day_warning(content: str) -> str | None:
    """手动模式 + 多目的地 → 天数 < 目的地数×2 时返回告警文案；否则 None 不打扰"""
    from app.services.express_planning_service import _split_cities
    # ★ 复用第 11 章的三个字符串助手：全项目只有一套"读表单"的写法
    from app.tools.state_transition import _between, _leading_int, _value_after
    head = _between(content, _HEAD_DEST_PREFIX, _HEAD_DEST_SUFFIX).strip()
    if not head:
        return None
    cities = _split_cities(head)
    if len(cities) <= 1:
        return None
    needed = len(cities) * 2  # 多目的地天数下限：每个目的地 2 天
    days = _leading_int(_value_after(content, _HEAD_DAYS_LABEL))
    if days is None or days >= needed:
        return None
    return (f"⚠️ 您选择了 {len(cities)} 个目的地，行程时间紧凑，至少需要 {needed} 天"
            f"，否则无法为您生成完整报告。"
            f"请将旅行天数调整为 {needed} 天及以上（或减少目的地）。")


def _looks_like_itinerary(text: str) -> bool:
    """多日行程看'第X天'标记；单日行程看时段块（上午/中午/下午/晚上，≥3 个）"""
    if not text or len(text) <= 150:
        return False
    if _DAY_MARK.search(text):
        return True
    # ★ 单日行程没有"第X天"，就数时段词够不够 3 个
    return len(re.findall(r"上午|中午|下午|晚上", text)) >= 3


async def _recent_itinerary_text(db: AsyncSession, conversation_id) -> str | None:
    """ 取历史里最近一条"像行程"的助手消息作为清单提炼源料

    场景：确认轮只回一句话（防重复输出），该文本无法作为提炼源料，
    真正的行程正文在上一轮的助手消息里（可能还含预算明细表，一并作为源料无妨）。
    只回看最近 8 条助手消息，找不到则返回 None（调用方据此不生成清单）
    """
    rows = (await db.execute(
        select(Message.content, Message.extra_info)
        .where(Message.conversation_id == conversation_id, Message.role == "assistant")
        .order_by(Message.created_at.desc())
        .limit(8)
    )).all()
    for content, extra in rows:
        # content 超出压缩阈值时只存摘要（列表预览用），全文在 extra_info["original"]，
        # 清单提炼必须用全文，否则源料不足会提炼出残缺行程
        text = (extra or {}).get("original") or content or ""
        if _looks_like_itinerary(text):
            return text
    return None

async def run_chat_turn(
        db: AsyncSession, user_id: str, conversation_id: str, content: str,
        force_planning: bool = False, persist_user: bool = True,
        resume_stage: str | None = None,
) -> EventStream:
    """单轮对话：立即返回事件流，生产任务后台执行（调用方须在请求周期内消费完）
    force_planning / persist_user：前端UI Skill卡片提交后的续跑专用参数。
    卡片提交属于独立REST动作，仅落状态不跑编排会造成流程停滞；续跑由代码指定走规划状态机，不依赖意图路由，
    同时不持久化合成发言，避免用户未输入的文本出现在对话历史。
    resume_stage：卡片提交后状态机推进到的实际步骤，反馈文案按此字段确定性生成，消除界面文案与真实流程不一致问题。
    """
    stream = EventStream()
    # 创建后台任务，不阻塞当前请求，实现流式边生成边推送
    asyncio.create_task(_produce(
        db, user_id, conversation_id, content, stream,
        force_planning=force_planning, persist_user=persist_user,
        resume_stage=resume_stage,
    ))
    return stream

# 卡片提交续跑提示文案：根据状态机当前步骤直接取值，不再依靠force_planning推断
_RESUME_PENDING_TEXT = {
    "accommodation_planning": "\U0001F684 车次已确认，正在为您规划住宿…\n",
    "food_planning": "住宿已确认，正在为您安排餐饮…\n",
    "itinerary_generation": "住宿已确认，正在为您生成行程报告…\n",
    "budget_summarization": "行程已生成，正在为您汇总预算…\n",
    "report_generation": "行程已生成，正在为您生成行程报告…\n",
}

async def _produce(
        db: AsyncSession, user_id: str, conversation_id: str, content: str, stream: EventStream,
        force_planning: bool = False, persist_user: bool = True,
        resume_stage: str | None = None,
) -> None:
    try:
        stream = _SkillUiRecorder(stream)
        # 会话标题：首条消息自动生成
        conv = await get_owned_conversation(db, conversation_id, user_id)
        if conv and conv.title == "新对话" and persist_user:
            conv.title = content[:30]
            await db.commit()

        # 保存用户消息
        if persist_user:
            db.add(Message(conversation_id=conversation_id, role="user", content=content))
            await db.commit()

        # 读取最近4条消息作为上下文
        hist_q = select(Message.role, Message.content).where(
            Message.conversation_id == conversation_id
        ).order_by(Message.created_at.desc()).limit(4)
        hist_rows = (await db.execute(hist_q)).all()
        history = [f"{r.role}: {r.content}" for r in reversed(hist_rows)]

        _auto_quick_hit = _hit_auto_confirm(content)
        if _auto_quick_hit:
            stream.push(make_event(ROLE_ASSISTANT, "✈️ 正在为您规划行程，请稍候…\n"))

        if force_planning:
            router = RouterResult(
                is_itinerary_planning=True,
                inference_log="[卡片提交] 车次已选定，续跑住宿步",
                skills=[],
            )
        else:
            router = await chat_route(content, history=history)

        # 直查票务确定性门禁，优先级高于意图路由
        _direct_text = None if force_planning else await try_direct_ticket_query(content, stream, conversation_id)

        # 推送规划提示文本
        if _direct_text is None and router.is_itinerary_planning and not _auto_quick_hit:
            _resume_text = _RESUME_PENDING_TEXT.get(resume_stage or "")
            stream.push(make_event(ROLE_ASSISTANT, _resume_text or (
                "\U0001F684 车次已确认，正在为您规划住宿…\n" if force_planning
                else "\u2708\ufe0f 正在为您规划行程，请稍候…\n"
            )))

        for s in router.skills:
            stream.push(make_event(ROLE_SKILLS, {
                "skill_name": s.skill_name, "title": s.title, "desc": s.desc,
            }))

        # 快速通道分流
        express_req = None
        express_sync = None
        if _direct_text is None and router.is_itinerary_planning and not force_planning:
            from app.services.express_planning_service import try_express_planning
            express_req = await try_express_planning(user_id, content, stream)

        # 执行分支：直查 / 快速通道 / 状态机
        if _direct_text is not None:
            full_text, state_step, committed, body = _direct_text, None, False, None
        elif express_req is not None:
            from app.services.express_planning_service import run_express_planning
            # 快速通道同样产出结构化逐日行程，交给 generate_plan 作清单兜底
            full_text, body, express_sync = await run_express_planning(express_req, stream)
            committed = False
            if full_text is None:
                app_logger.info("[express] 极速模式未接管，回落状态机")
                full_text, state_step, committed, body = await execute_turn(
                    user_id, conversation_id, content, router.is_itinerary_planning, stream
                )
            else:
                state_step = "itinerary_generation"
        else:
            _warn = _manual_multi_city_day_warning(content)
            if _warn:
                stream.push(make_event(ROLE_ASSISTANT, _warn + "\n\n"))
            full_text, state_step, committed, body = await execute_turn(
                user_id, conversation_id, content, router.is_itinerary_planning, stream
            )
            if _warn:
                full_text = f"{_warn}\n\n{full_text or ''}"

        # 行程清单生成逻辑
        plan_id = None
        _auto_confirm_mode = _auto_quick_hit or express_req is not None
        _ready_steps = _ITINERARY_READY_STEPS if _auto_confirm_mode else {
            "budget_summarization", "report_generation"
        }

        _source = full_text if _looks_like_itinerary(full_text) else (
            await _recent_itinerary_text(db, conversation_id) if committed else None
        )
        if not _source and body:
            _source = full_text or ""

        if (router.is_itinerary_planning
                and state_step in _ready_steps
                and (_source or body)):
            stream.push(make_event(ROLE_ITINERARY, {
                "status": "pending", "id": "", "message": "行程清单生成中...",
            }))
            try:
                plan_id = await generate_plan(db, conversation_id, _source or "", structure=body)
                stream.push(make_event(ROLE_ITINERARY, {
                    "status": "success", "id": plan_id, "message": "行程清单已生成，点击查看！",
                }))
            except Exception:
                app_logger.exception("行程清单生成失败")
                stream.push(make_event(ROLE_ITINERARY, {
                    "status": "error", "id": "", "message": "行程清单生成失败",
                }))

        # 快速通道短期记忆同步：**与清单是否生成成功解耦**
        # 此前它写在 generate_plan 的 try 里，清单一失败就整段跳过，
        # thread 里留着上一轮的旧目的地，下一轮天气/行程都按旧状态渲染
        if express_req is not None:
            try:
                from app.services.express_planning_service import sync_express_state
                await sync_express_state(
                    conversation_id, express_req, full_text or "", plan_id, express_sync
                )
            except Exception:
                app_logger.exception("[express] 状态机短期记忆同步失败（不影响本轮回复）")
        stream.push(make_event(ROLE_DONE, "finish", code=200))

        # 消息落库，超长内容压缩
        saved_text = full_text
        if router.is_itinerary_planning and len(full_text) > COMPRESS_THRESHOLD:
            saved_text = await compress_content(full_text)
        extra: dict = {"original": full_text, "inference_steps": router.inference_log}
        if stream.skill_ui:
            extra["skill_ui"] = stream.skill_ui
        if plan_id:
            extra["itinerary_plans"] = {"status": "success", "id": plan_id, "message": "行程清单已生成，点击查看！"}
        db.add(Message(
            conversation_id=conversation_id,
            role="assistant",
            content=saved_text,
            extra_info=extra,
        ))
        await db.commit()
    except Exception as e:
        app_logger.exception("对话服务异常")
        stream.push(make_event(ROLE_ERROR, str(e)))
        stream.push(make_event(ROLE_DONE, "finish", code=500))
    finally:
        stream.close()


