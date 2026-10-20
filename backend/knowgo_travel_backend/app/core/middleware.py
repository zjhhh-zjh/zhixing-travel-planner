from jinja2 import Template
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import HumanMessage, ToolMessage
from typing import Callable, Any
from app.core.state import TravelState
from app.core.store import get_user_memory_service
from app.utils.logger import app_logger

def _leg_query_date(state: dict, legs: list, cursor: int) -> str:
    """ 多段行程的"段日期"按时序分配（代码算，不由模型猜）

    实测多城会话三段城际全按出发日查询（重庆→北京、北京→天津都查 2026-09-18），
    与实际时序矛盾（人不可能同一天既在重庆又在北京又在天津）。规则：
      ① 出发地→首城 = 出发日；
      ② 城市 i-1 → 城市 i = 出发日 + 前 i 站累计晚数（即下一城的首日）；
      ③ 返程段（末段、终点=出发地）= 末日。
    晚数分配与酒店侧同源：出发地 1 晚 + 剩余天数在其后城市均分
    """
    from datetime import date as _date, timedelta as _timedelta
    req = state.get("user_requirement") or {}
    days = int(req.get("travel_days") or 0)
    try:
        start = _date.fromisoformat(str(req.get("departure_date") or "")[:10])
    except Exception:
        return ""
    if days <= 0 or not legs:
        return ""
    c = max(0, min(int(cursor), len(legs) - 1))
    from app.services.express_planning_service import _stay_nights, ordered_play_cities
    # 与住宿站点同源对齐——节点 = ordered_play_cities（目的地城市串，
    # 出发地仅当本身也在目的地串里才计入并置最前），晚数按**真实节点数**分配（此前按
    # legs+1 估，3 城以上与酒店侧错位）。出发地本身是游玩首节点时（北京→北京/天津），
    # 首段（北京→天津）发生在"北京住 1 晚之后"，偏移整体 +1 站
    dep = str(req.get("departure_city") or "").strip()
    dest = str(state.get("selected_destination") or "") or str(req.get("destination") or "")
    # ★ 返程段 = 末日：单独判定，不靠最后一行 min(offset, days - 1) 的截断碰巧落到末日
    if dep and c == len(legs) - 1 and str(legs[c]).rpartition("→")[2].strip() == dep:
        return (start + _timedelta(days=days - 1)).isoformat()
    nodes = ordered_play_cities(dep, dest)
    # ★ 与住宿步同一口径：首站 1 晚、其余均分
    nights = _stay_nights(days, len(nodes) or (len(legs) + 1))
    # ★ 出发地也在目的地里：第一站在本地玩，每段都往后挪一站
    shift = 1 if (dep and dep in nodes and nodes[0] == dep) else 0
    offset = sum(nights[:min(c + shift, len(nights))]) if nights else 0
    return (start + _timedelta(days=min(offset, days - 1))).isoformat()

# 本地步骤切换工具：调用即代表本步骤结束，进入下一步骤
_LOCAL_STEP_TOOLS = {
    "record_requirement_tool",
    "select_destination_tool",
    "select_transport_tool",
    "select_accommodation_tool",
    "select_accommodation_food_tool",
    "select_food_tool",
    "generate_itinerary_tool",
    "submit_itinerary_body_tool",
    "summarize_budget_tool",
}

# 无轮次上限步骤：允许模型完整跑完生成逻辑，等待用户交由prompt与清单门禁控制
_NO_ROUND_CAP_STEPS = {
    "requirement_collection",
    "itinerary_generation",
    "budget_summarization",
    "report_generation",
}

# 手动模式单次发言最多允许的数据查询轮数，超出则清空工具表收尾
_MANUAL_MAX_QUERY_ROUNDS = 2

from app.core.intent import detect_intent


def _is_confirm_reply(text: str) -> bool:
    """判断是否为纯确认语，仅简单肯定回复命中；带修改需求的回复不算确认"""
    return detect_intent(text).is_confirm_reply


# 手动模式各步骤对应的选择工具
_MANUAL_STEP_SELECT = {
    "destination_recommendation": "select_destination_tool",
    "transport_planning": "select_transport_tool",
    "accommodation_planning": "select_accommodation_tool",
    "food_planning": "select_food_tool",
}

# 手动确认模式：一轮一步 + 确认闸门规则
_MANUAL_STEP_MODE = (
    "【手动确认模式 - 一轮一步 + 确认闸门】本步骤严格按下面三种轮次之一作答，禁止跨步骤混写："
    "① 候选轮：只展示本步骤候选清单，结尾请用户挑选，本轮结束；严禁夹带后续步骤内容。"
    "② 详单轮：用户选定对象后，只展示该对象详单，结尾询问是否确认；本轮select_*工具被屏蔽，不要调用，不得标记已锁定确认。"
    "③ 确认轮：用户回复纯确认语时，调用对应select_*工具记录并推进，同轮展示下一步候选清单。"
    "**判断依据**：工具列表无select_*代表非确认轮，仅允许查询展示，不可声称完成选择或跳过确认。"
    "**重要**：无可用查询工具时，基于已有结果输出候选清单并结束本轮，禁止反复查询或报错；同一轮每个候选清单仅输出一次。"
)

# 自动确认模式规则
_AUTO_CONFIRM_MODE = (
    "【自动确认模式】AI自动选定推进，无需询问用户。"
    "**同一回合仅允许输出一次完整行程正文+一次预算明细表**：回合内多轮工具迭代时，中间轮只写一句话说明进展，"
    "禁止输出Day详情和预算表；仅最终收尾轮一次性输出最新版完整行程与预算。"
)

# 产出型步骤标识
_PRODUCE_STEPS = ("itinerary_generation", "budget_summarization")

# 手动模式产出步骤规则
_MANUAL_PRODUCE_MODE = (
    "【手动确认模式 - 产出步骤】本步骤无select_*工具，不等待候选挑选确认，按两类用户输入处理："
    "① 用户表示满意/确认：调用submit_itinerary_body_tool提交定稿落库；"
    "② 用户提出结构性变更：调用summarize_budget_tool重算预算，再提交新版完整结构，无需二次确认。"
    "两类场景同一轮完成「调预算 → 提交结构」，不要自行编写正文，正文预算由系统渲染。"
    "收尾固定措辞：「如需调整（天数/预算/目的地等），直接告诉我即可。」禁止额外确认反问。"
)
import re
_DAY_MARK = re.compile(r"第\s*(\d+|[一二三四五六七八九十]+)\s*天|Day\s*\d", re.IGNORECASE)
def _history_has_itinerary(msgs: list) -> bool:
    """判断本回合历史里是否已输出过逐日行程正文
    行程轮规则：先展示正文+预算征询、用户满意后才落库，确认轮的必要条件是
    历史里存在 Day X 正文；若没有（例如用户第一轮直接说"满意"），不能强制落库
    """
    for m in msgs:
        if getattr(m, "type", None) != "ai":
            continue
        content = getattr(m, "content", "") or ""
        if isinstance(content, str) and _DAY_MARK.search(content):
            return True
    return False

_TOOL_ABORTED_HINT = (
    "【系统提示】该工具调用未执行完成（回合内被中断），没有可用结果，请勿据此作答。"
)

def _repair_dangling_tool_calls(msgs: list) -> tuple[list, int, int]:
    """修复悬空 tool_calls / 孤立 tool 消息
    返回 (处理后的消息列表, 补入的 ToolMessage 条数, 丢弃的孤立 ToolMessage 条数)；
    无需修复时原样返回入参对象（零拷贝，不影响正常路径）
    """
    out: list = []
    inserted = dropped = 0
    i, n = 0, len(msgs)
    while i < n:
        m = msgs[i]
        mtype = getattr(m, "type", None)
        if mtype == "ai" and getattr(m, "tool_calls", None):
            out.append(m)
            # 记录本AI消息的tool_call_id，后续匹配已响应的工具消息
            pending = {
                tc.get("id"): tc.get("name")
                for tc in m.tool_calls if tc.get("id")
            }
            j = i + 1
            while j < n and getattr(msgs[j], "type", None) == "tool":
                pending.pop(getattr(msgs[j], "tool_call_id", None), None)
                out.append(msgs[j])
                j += 1
            # 对未得到响应的tool_call，补入占位ToolMessage
            for tid, tname in pending.items():
                out.append(ToolMessage(
                    content=_TOOL_ABORTED_HINT, tool_call_id=tid, name=tname or "tool"
                ))
                inserted += 1
            i = j
            continue
        # 没有对应AI tool_call的孤立tool消息，直接丢弃
        if mtype == "tool":
            dropped += 1
            i += 1
            continue
        out.append(m)
        i += 1
    if inserted or dropped:
        return out, inserted, dropped
    return msgs, 0, 0


# ============== 上下文窗口兜底（按"用户轮"边界丢弃最旧历史） ==============
# 仅裁剪发往模型的视图，checkpoint持久化历史完整保留；
# 切点只在HumanMessage边界，不切割AI-Tool消息配对，避免产生残缺工具历史
_WINDOW_TRIGGER = 40   # 消息数超过该阈值才执行裁剪
_WINDOW_KEEP = 24      # 目标最少保留消息条数

def _trim_window(msgs: list) -> list:
    """按用户轮边界丢弃最旧历史，找不到安全切点则原样返回"""
    if len(msgs) <= _WINDOW_TRIGGER:
        return msgs
    last_human = -1
    for i in range(len(msgs) - 1, -1, -1):
        if isinstance(msgs[i], HumanMessage):
            last_human = i
            break
    if last_human <= 0:
        # 无更早可裁剪的用户轮
        return msgs
    # 寻找裁剪切点
    cut = min(len(msgs) - _WINDOW_KEEP, last_human - 1)
    for i in range(cut, -1, -1):
        if isinstance(msgs[i], HumanMessage):
            kept_system = [m for m in msgs[:i] if getattr(m, "type", None) == "system"]
            return kept_system + msgs[i:]
    return msgs


class StepConfigMiddleware(AgentMiddleware):
    """
    步骤配置中间件 - 根据 current_step 动态配置 Agent
    """

    def __init__(self, step_config: dict):
        """
        初始化中间件

        Args:
            step_config: 预加载的步骤配置字典
        """
        self._step_config = step_config

    async def awrap_model_call(
            self,
            request: ModelRequest,
            handler: Callable[[ModelRequest], ModelResponse]
    ) -> ModelResponse:
        """
        根据 current_step 动态配置 Agent
        """
        # 获取当前步骤
        state: TravelState = request.state
        # 首先state里面判断有没有items属性，有和没有都转为字典
        state_dict = dict(state) if hasattr(state, "items") else {}
        # ★ 看点①：读状态——当前步骤、用户；表单多选字段先规整成列表，模板渲染要用
        current_step = state.get("current_step", "requirement_collection")
        user_id = state.get("user_id")

        # 兼容历史 checkpoint：早期记录的需求里没有预选字段，
        # 模板中 {{ user_requirement.xxx }} 取不到值会抛 UndefinedError，此处补齐默认空列表
        _req = state_dict.get("user_requirement")
        if isinstance(_req, dict):
            _req = dict(_req)
            for _k in ("transport_types", "food_preferences", "accommodation_types"):
                _req.setdefault(_k, [])
            state_dict["user_requirement"] = _req

        app_logger.info(f"📋 用户ID: {user_id}")
        app_logger.info(f"📍 当前步骤: {current_step}")

        if current_step not in self._step_config:
            app_logger.error(f"❌ 未知步骤: {current_step}")
            raise ValueError(f"未知步骤: {current_step}")

        # ★ 看点②：取本步配置并校验前置字段（第 4 章的骨架）
        step_config = self._step_config[current_step]

        # ========== 验证前置依赖 ==========
        for required_field in step_config["requires"]:
            if required_field not in state or state[required_field] is None:
                error_msg = f"步骤 {current_step} 需要完整状态: {required_field} 未设置"
                app_logger.error(f"❌ {error_msg}")
                raise ValueError(error_msg)

        # ========== 🔑 核心：注入长期记忆 ==========
        # ★ 看点③：长期记忆注入（第 5 章）
        memory_prompt = ""
        if user_id:
            try:
                service = await get_user_memory_service()
                memory_prompt = await service.format_memory_for_prompt(user_id)
                if memory_prompt:
                    app_logger.info(f"💾 已加载用户长期记忆: {user_id}")
                else:
                    app_logger.info(f"📝 用户首次使用，暂无历史记忆: {user_id}")
            except Exception as e:
                app_logger.warning(f"⚠️ 加载长期记忆失败: {e}")

        # ========== 动态填充提示词变量 ==========
        try:
            state_dict["user_memory"] = memory_prompt  # 将记忆注入到模板变量
            # 注入确认模式语义变量：把状态里的 auto_confirm 渲染成人可读的强约束文案。
            # 此前各步骤用"需求清单含『全程交给 AI 自动确认』时…"做条件判断——模型对"缺失"不可靠，
            # 实测手动模式仍被自动选定交通/住宿/餐饮；改为状态驱动后模式恒定明确
            # ★ 看点④：确认模式三选一（自动确认 / 产出步骤 / 手动确认），渲染进步骤 prompt
            if state.get("auto_confirm"):
                # 自动模式文案抽为模块级常量，并补"同一回合只输出一次
                # 完整正文 + 一次预算表"硬约束（详见 _AUTO_CONFIRM_MODE 处注释）
                state_dict["auto_confirm_mode"] = _AUTO_CONFIRM_MODE
            elif current_step in _PRODUCE_STEPS:
                # 产出型步骤改用专用文案——这两个步骤没有 select_* 工具，
                # 通用文案的"尚未选择 → 只许展示不许推进"会让模型把产出轮误判成等待轮，
                # 表现为输出完"正文 + 预算表"就反问「请确认」并停轮（清单因此要拖到下一轮才生成）
                state_dict["auto_confirm_mode"] = _MANUAL_PRODUCE_MODE
            else:
                # 通用手动文案升级为"一轮一步 + 确认闸门"三态契约
                state_dict["auto_confirm_mode"] = _MANUAL_STEP_MODE
            # 段级循环进度注入（多目的地 → "第 i/n 段：A→B"；单城为空串）。
            # 让模型只展示"当前待确认的那一段"，流程账本由段游标（代码）维护，不交给模型
            # ★ 看点⑤：进度提示——交通步告诉模型"第几段"，住宿步告诉它"第几城"
            leg_progress = ""
            if current_step == "transport_planning":
                from app.tools.state_transition import _expected_legs
                _legs = _expected_legs(state)
                if _legs:
                    _cur = max(0, min(int(state.get("leg_cursor") or 0), len(_legs) - 1))
                    leg_progress = (
                        f"第 {_cur + 1}/{len(_legs)} 段：{_legs[_cur]}"
                        f"（共 {len(_legs)} 段，含返程；本轮只展示并确认这一段）"
                    )
            state_dict["leg_progress"] = leg_progress
            # 住宿逐城循环进度（与交通段级循环对称）
            stay_progress = ""
            if current_step == "accommodation_planning":
                from app.tools.state_transition import _multi_city_names
                _cities = _multi_city_names(state)
                if _cities:
                    _sc = max(0, min(int(state.get("stay_cursor") or 0), len(_cities) - 1))
                    stay_progress = (
                        f"第 {_sc + 1}/{len(_cities)} 城：{_cities[_sc]}"
                        f"（共 {len(_cities)} 城；本轮只查询并展示这一城的酒店）"
                    )
            state_dict["stay_progress"] = stay_progress
            template = Template(step_config["prompt"])
            system_prompt = template.render(**state_dict)
            #**state_dict 的作用：这里的 ** 是 Python 的解包操作，
            #它告诉 Jinja2：“把这个字典里的所有 Key 都当作变量名，Value 都当作变量值去替换模板里的内容。”

            # 如果有长期记忆，追加到提示词末尾
            if memory_prompt:
                system_prompt = f"{system_prompt}\n\n{memory_prompt}"

        except KeyError as e:
            app_logger.warning(f"⚠️ 提示词变量缺失: {e}, 使用原始模板")
            system_prompt = step_config["prompt"]

        # ========== 手动模式硬闸门：用户未发言时屏蔽 select_* 推进工具 ==========
        # 问题：单轮内 Agent 会连续"查询→展示→推进"一路跑完全流程，仅靠 prompt 约束无效
        #（实测手动模式仍未询问就自动选定高铁/酒店）。改为确定性拦截：
        # 把拦截口径从"用户刚发言就放行"收紧为"用户刚回复纯确认语才放行"——
        # 用户说"G1630吧"这类**选择语**时同样屏蔽，迫使模型先输出"详单 + 请确认"，
        # 等用户下一轮回确认语（确认/确定/好的…）才放行推进，实现参考会话的确认闸门节奏
        # ★ 看点⑥：从这里开始按条件裁剪工具表，并决定要不要强推某个工具
        tools = step_config["tools"]
        forced_tool_choice: Any | None = None

        # ========== 行程步"正文必须落库"（确定性强制，模式无关） ==========
        # 实测缺陷：住宿卡片提交后进入行程步，模型只调 summarize_budget_tool 就收尾，
        # submit_itinerary_body_tool 从未被调用 → itinerary_body 为空 → 正文与"查看行程清单"
        # 按钮都出不来（用户以为选完酒店就没反应了）。prompt 约束不可靠，改为确定性拦截：
        # 本步未落库正文前，强制 tool_choice=submit_itinerary_body_tool（预算由该工具就地补算，
        # 见）；同一回合已尝试过提交（无论成败）即不再强推，避免校验失败时反复重试打转
        # ★ 看点⑦：行程步还没交结构化数据 → 强推 submit_itinerary_body_tool
        if current_step == "itinerary_generation" and not state.get("itinerary_body"):
            _submitted = False
            for _m in reversed(state.get("messages") or []):
                if getattr(_m, "type", None) == "human":
                    break
                if (getattr(_m, "type", None) == "tool"
                        and getattr(_m, "name", "") == "submit_itinerary_body_tool"):
                    _submitted = True
                    break
            if (not _submitted
                    and any(getattr(t, "name", "") == "submit_itinerary_body_tool"
                            for t in tools)):
                forced_tool_choice = "submit_itinerary_body_tool"
                app_logger.info("🔒 行程步：强制调用 submit_itinerary_body_tool 落库正文")

        # ★ 看点⑧：手动确认闸门——用户没说"确认"就摘掉 select_* 推进工具；查询轮数封顶
        if not state.get("auto_confirm"):
            msgs = state.get("messages") or []
            last_is_human = bool(msgs) and getattr(msgs[-1], "type", None) == "human"
            last_text = str(getattr(msgs[-1], "content", "") or "") if last_is_human else ""
            user_confirmed = _is_confirm_reply(last_text)
            # 放行名单：仅"用户本轮回复纯确认语"时放行推进工具（=确认轮）
            allowed_select = set()
            if last_is_human and user_confirmed:
                allowed_select |= {
                    "select_destination_tool",
                    "select_transport_tool",
                    "select_accommodation_tool",
                    "select_food_tool",
                }
            # 例外：餐饮偏好在表单已勾选 → 放行 select_food_tool（用户事先已选过，
            # 若一并屏蔽会让流程卡在餐饮轮反复询问，正是用户反馈的重复询问缺陷）
            if (state_dict.get("user_requirement") or {}).get("food_preferences"):
                allowed_select.add("select_food_tool")
            # 注意：select_accommodation_food_tool 是自动确认专用组合工具（一次性写入住宿+餐饮
            # 并跳过餐饮轮），手动模式一律不放行，避免用户失去选择权
            kept = [
                t for t in tools
                if not getattr(t, "name", "").startswith("select_")
                or getattr(t, "name", "") in allowed_select
            ]
            # 轮次上限（兜底，替代不可靠的工具名屏蔽）：
            # 模型被剥夺推进工具后会逐个换用还没用过的工具继续 churn（实测交通轮连查 6 轮、
            # 最后把 get-tickets 参数调空仍继续），因此以"轮数"封顶，与工具名解耦。
            # 两个关键修正：
            #   ① 计数按步骤归零——倒序扫描遇到本地步骤切换工具（select_* 等）即停止，
            #      避免把上一步的 select 调用算进本步骤，导致住宿轮只够调"偏好记忆"就没了工具、
            #      进而凭空调出编造酒店列表；
            #   ② 行程生成/预算等产出型步骤不设上限——清空工具表会破坏正文与预算产出
            if not last_is_human and current_step not in _NO_ROUND_CAP_STEPS:
                rounds = 0
                for m in reversed(msgs):
                    if getattr(m, "type", None) == "human":
                        break
                    if getattr(m, "type", None) == "ai" and getattr(m, "tool_calls", None):
                        names = {tc.get("name") for tc in m.tool_calls}
                        if names & _LOCAL_STEP_TOOLS:
                            break  # 该轮为步骤切换，之前的轮次属于上一步骤，不计入
                        rounds += 1
                if rounds >= _MANUAL_MAX_QUERY_ROUNDS:
                    app_logger.info(
                        f"🔒 手动模式闸门：{current_step} 已调用 {rounds} 轮查询工具，"
                        f"清空工具表强制输出清单收尾"
                    )
                    kept = []
            if len(kept) != len(tools):
                app_logger.info(
                    f"🔒 手动模式闸门：用户尚未确认，最终可用 {len(kept)}/{len(tools)} 个工具，等待选择"
                )
            tools = kept

            # ========== 确认轮强制推进 ==========
            # 用户明确回复确认语（确认/确定/好的…）时，确定性调用本步骤的 select_* 工具，
            # 避免模型只回一句"已确认"却不动状态机、把会话卡在本步骤（与同源问题：
            # prompt 要求"确认后调用工具"不可靠，改由代码兜底）
            forced_name = _MANUAL_STEP_SELECT.get(current_step)
            if (last_is_human and user_confirmed and forced_name
                    and any(getattr(t, "name", "") == forced_name for t in tools)):
                forced_tool_choice = forced_name
                app_logger.info(f"🔒 手动模式确认轮：强制调用 {forced_name} 推进到下一步")

            # ========== 手动模式"确认轮"强制调用落库工具 ==========
            # 问题：行程轮规则 5/6 要求"用户回复满意后必须调用 generate_itinerary_tool 落库"，
            # 但实测模型会只回一句"好的，已按此方案生成行程清单…"而不真正调用工具（日志实证：
            # 用户说"满意"的那轮工具未被调用，状态停在 itinerary_generation，清单门禁不放行，
            # 用户永远拿不到清单；追问"然后呢？"时模型才补调工具）。prompt 约束不可靠，
            # 改为确定性拦截：手动模式 + 行程生成步 + 用户刚发言且是纯确认语 + 历史已展示过
            # 逐日正文 + 工具表里确有落库工具 → 本轮强制 tool_choice=generate_itinerary_tool，
            # 保证状态机必然推进到 budget_summarization（清单门禁随即可放行）
            if (current_step == "itinerary_generation"
                    and last_is_human
                    and _is_confirm_reply(
                        str(getattr(msgs[-1], "content", "") or "")
                    )
                    and _history_has_itinerary(msgs)
                    and any(getattr(t, "name", "") == "generate_itinerary_tool"
                            for t in tools)):
                forced_tool_choice = "generate_itinerary_tool"
                app_logger.info("🔒 手动模式确认轮：强制调用 generate_itinerary_tool 落库")

        # ========== 多段高铁：模型不再自查，班次改由代码并行供给 ==========
        # 交通步走"多段高铁"路线时（手动模式前端会拿到选择卡片、自动模式由代码代选），
        # 从工具表里摘掉 12306 查询工具：否则模型仍会照旧规则"逐段串行查"，既重复请求，
        # 又把自编班次写进正文（同一屏出现两套互相矛盾的车次/票价）
        # ★ 看点⑨：交通步接管——高铁 > 航班 > 自驾，走代码卡片时摘掉模型自带的查询 / 推进工具
        if current_step == "transport_planning":
            from app.capabilities.train_multi_segment.train_flow import multi_leg_train
            from app.services.express_planning_service import _split_cities
            from app.tools.state_transition import _expected_legs
            # 零段直通——出发地与目的地同城（本地游玩）时没有任何城际段，
            # 也把全部交通查询工具摘掉，避免模型空转查询无效班次或编造车次
            _dep = str(((state.get("user_requirement") or {}).get("departure_city")) or "").strip()
            _zero_leg = bool(
                _dep and _split_cities(state.get("selected_destination") or "")
                and not _expected_legs(state)
            )
            if multi_leg_train(state) or _zero_leg:
                _marks = ("train", "ticket", "railway") + (
                    ("flight", "driving") if _zero_leg else ()
                )
                _before = len(tools)
                tools = [
                    t for t in tools
                    if getattr(t, "name", "") != "select_transport_tool"
                    and not any(k in getattr(t, "name", "").lower() for k in _marks)
                ]
                # 强推的工具若已被摘掉（确认轮原本会强推 select_transport_tool），必须取消强推，
                # 否则模型调用一个不在工具表里的工具 → 请求直接报错
                if forced_tool_choice and forced_tool_choice not in {
                    getattr(t, "name", "") for t in tools
                }:
                    forced_tool_choice = None
                if len(tools) != _before:
                    _why = ("零段直通（出发地=目的地，无城际段）" if _zero_leg
                            else "多段高铁：班次由代码并行查询供给")
                    app_logger.info(
                        f"🚄 交通步：屏蔽模型自带查询/推进工具 {_before - len(tools)} 个（{_why}）"
                    )
            else:
                # ========== 多段航班：模型不再自查，航班改由代码并行供给 ==========
                # 交通步走"航班卡片"路线时（手动模式前端拿到选择卡片、自动模式由代码代选），
                # 从工具表里摘掉航班查询工具与 select_transport_tool：否则模型仍会照旧规则
                # 逐段查航班，既重复请求，又把自编航班写进正文（同一屏两套矛盾数据）。
                # 用 elif 语义（放在高铁之后）：同时勾选高铁与航班时高铁优先，此处不接管
                from app.capabilities.flight_multi_segment.flight_flow import flight_card_mode
                if flight_card_mode(state):
                    _before = len(tools)
                    tools = [
                        t for t in tools
                        if getattr(t, "name", "") != "select_transport_tool"
                        and "flight" not in getattr(t, "name", "").lower()
                    ]
                    # 强推的工具若已被摘掉（确认轮原本会强推 select_transport_tool），必须取消强推，
                    # 否则模型调用一个不在工具表里的工具 → 请求直接报错
                    if forced_tool_choice and forced_tool_choice not in {
                        getattr(t, "name", "") for t in tools
                    }:
                        forced_tool_choice = None
                    if len(tools) != _before:
                        app_logger.info(
                            f"✈️ 交通步：屏蔽模型自带查询/推进工具 {_before - len(tools)} 个"
                            f"（多段航班：航班由代码并行查询供给）"
                        )
                else:
                    # ========== 多段自驾：模型不再自查，路线改由代码并行供给 ==========
                    # 交通步走"自驾确认卡片"路线时（手动模式前端拿到只读确认卡片、自动模式由代码
                    # 直接落库推进），从工具表里摘掉驾车查询工具与 select_transport_tool：否则模型
                    # 仍会照旧规则逐段查路线，既重复请求，又把自编里程/耗时写进正文（两套矛盾数据）。
                    # 用 elif 语义（放在高铁、航班之后）：优先级 高铁 > 航班 > 自驾，此处不越位接管
                    from app.capabilities.driving_multi_segment.driving_flow import driving_card_mode
                    if driving_card_mode(state):
                        _before = len(tools)
                        _driving_marks = ("driving", "drivingroute")
                        tools = [
                            t for t in tools
                            if getattr(t, "name", "") != "select_transport_tool"
                            and not any(k in getattr(t, "name", "").lower()
                                        for k in _driving_marks)
                        ]
                        # 强推的工具若已被摘掉（确认轮原本会强推 select_transport_tool），必须取消强推，
                        # 否则模型调用一个不在工具表里的工具 → 请求直接报错
                        if forced_tool_choice and forced_tool_choice not in {
                            getattr(t, "name", "") for t in tools
                        }:
                            forced_tool_choice = None
                        if len(tools) != _before:
                            app_logger.info(
                                f"🚗 交通步：屏蔽模型自带查询/推进工具 {_before - len(tools)} 个"
                                f"（多段自驾：路线由代码并行查询供给）"
                            )

        # ========== 多段住宿：酒店候选改由代码并行供给 ==========
        # 住宿步走"卡片一次性选择"路线时（手动模式前端拿到选择卡片、自动模式由代码代选），
        # 从工具表里摘掉酒店查询工具与逐城推进工具：否则模型仍会照旧规则"逐城串行查"，
        # 既重复打酒店 MCP，又把自编酒店清单写进正文（同一屏出现两套互相矛盾的酒店/价格）
        # ★ 看点⑩：住宿步接管——hotel_card_mode 时摘掉酒店查询与推进工具
        if current_step == "accommodation_planning":
            from app.capabilities.hotel_multi_segment.hotel_flow import hotel_card_mode
            if hotel_card_mode(state):
                _before = len(tools)
                tools = [
                    t for t in tools
                    if getattr(t, "name", "") not in {
                        "select_accommodation_tool", "select_accommodation_food_tool",
                    }
                    and not any(k in getattr(t, "name", "").lower()
                                for k in ("hotel", "hotels"))
                ]
                # 强推的工具若已被摘掉（确认轮原本会强推 select_accommodation_tool），必须取消强推，
                # 否则模型调用一个不在工具表里的工具 → 请求直接报错
                if forced_tool_choice and forced_tool_choice not in {
                    getattr(t, "name", "") for t in tools
                }:
                    forced_tool_choice = None
                if len(tools) != _before:
                    app_logger.info(
                        f"🏨 多段住宿：屏蔽模型自带酒店查询/逐城推进工具 {_before - len(tools)} 个"
                        f"（候选由代码并行查询供给，前端卡片一次选完提交）"
                    )

        # ========== 残缺工具调用历史自愈（避免 400 卡死整个会话） ==========
        # ========== 上下文窗口兜底（按用户轮边界裁掉最旧历史轮） ==========
        # 只在检出残缺/需要裁窗口时才重建请求（正常路径零拷贝，不进 override）
        # ★ 看点⑪：历史自愈——补悬空工具调用 + 整轮裁窗口，只改本次请求、不动 checkpoint
        req_messages = list(getattr(request, "messages", None) or [])
        rebuilt = False
        if req_messages:
            req_messages, patched_in, patched_out = _repair_dangling_tool_calls(req_messages)
            if patched_in or patched_out:
                app_logger.warning(
                    f"🩹 检测到残缺工具调用历史：补入 {patched_in} 条 ToolMessage、"
                    f"丢弃 {patched_out} 条孤立 ToolMessage（仅修本次请求，不改 checkpoint）"
                )
                rebuilt = True
            windowed = _trim_window(req_messages)
            if len(windowed) != len(req_messages):
                app_logger.info(
                    f"🪟 上下文窗口裁剪：丢弃最旧 {len(req_messages) - len(windowed)} 条消息"
                    f"（按用户轮边界对齐，保留 {len(windowed)} 条；仅改本次请求，checkpoint 不动）"
                )
                req_messages = windowed
                rebuilt = True
            if rebuilt:
                request = request.override(messages=req_messages)

        # ========== 注入配置 ==========
        # ★ 看点⑫：一次性覆盖 system_prompt + tools +（可选）tool_choice
        modified_request = request.override(
            system_prompt=system_prompt,
            tools=tools,
            **({"tool_choice": forced_tool_choice} if forced_tool_choice else {})
        )

        app_logger.info(f"✅ 已注入步骤配置: {len(tools)} 个工具")

        # 写入本轮工具入参兜底上下文（出发地/目的地/出发日期/是否多段），
        # 供工具层在模型漏传必填参数时确定性回填——提示词约束不可靠，改由代码层保证
        # ★ 看点⑬：把本轮出发地 / 目的地 / 段日期 / 当前段写进 contextvar，车票守卫据此回填入参
        from app.tools.mcp_tools import set_turn_context
        from app.tools.state_transition import _expected_legs as _legs_of
        _treq = state.get("user_requirement") or {}
        _dest = str(state.get("selected_destination") or "")
        # 交通步的查询目标 = 段游标指向的当前段（而非目的地首城），
        # 保证模型漏传/写错参数时确定性回填到"正在确认的那一段"，一次只查一段
        _leg_from = _leg_to = ""
        _all_legs = _legs_of(state)
        if current_step == "transport_planning" and _all_legs:
            _leg_cur = max(0, min(int(state.get("leg_cursor") or 0), len(_all_legs) - 1))
            _leg_from, _leg_to = _all_legs[_leg_cur].split("→")
        try:
            set_turn_context(
                departure_city=str(_treq.get("departure_city") or ""),
                destination=_dest.split("、")[0].split(",")[0].strip(),
                # 交通步的查询日期按"段"给，而非一律出发日（多段时序分配）
                date=(_leg_query_date(state, _all_legs, state.get("leg_cursor") or 0)
                      if current_step == "transport_planning" else "")
                or str(_treq.get("departure_date") or ""),
                multi_leg=len(_all_legs) > 1,  # 改为按段数判定——单城亦有 2 段（去程+返程），返程查询同样须按段游标回填起讫站
                leg_from=_leg_from,
                leg_to=_leg_to,
            )
        except Exception as _e:
            app_logger.warning(f"⚠️ 写入工具入参上下文失败（不影响主流程）: {_e}")

        return await handler(modified_request)


async def create_step_config_middleware() -> StepConfigMiddleware:
    """
    工厂函数：创建步骤配置中间件

    Returns:
        预加载配置的 StepConfigMiddleware 实例
    """
    from app.agents.handoffs.step_config import get_step_config

    step_config = await get_step_config()
    return StepConfigMiddleware(step_config)