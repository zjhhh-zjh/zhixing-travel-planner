"""多段高铁 UI Skill —— 与状态机「交通规划步」的接线层

背景：交通步原本是**逐段串行**流程（prompt 驱动：一次只查一段、只展示一段、用户确认一段），
段账本由 select_transport_tool 的 leg_cursor/leg_selections 维护，多段行程要来回 N 轮。
本模块把这段循环换成「代码一次并行查全部段 → 前端卡片一次选完 → 一次提交」：

  1. emit_train_skill_ui   手动确认模式：并行查询全部段 → 推 skill_ui 帧（前端内嵌卡片）
  2. auto_select_train_legs 自动确认模式：代码按「有票优先 → 耗时 → 价格」代选并推进（不弹 UI）
  3. apply_train_selection  前端提交 → 基础合法性校验（**不做任何时间校验**）→ 写段账本 → 推进

硬约束（与用户要求一致）：全流程不比对到站/发车先后，不判断中转时间是否够。
"""
from typing import Any, Optional

from app.core.events import ROLE_SKILL_UI, make_event
from app.capabilities.base import SignatureCache, duration_to_minutes, get_agent_and_state, leg_signature
from app.capabilities.train_multi_segment.train_agent_tool import (
    build_train_multi_segment_payload,
    get_cached_payload,  # 读取提交侧校验缓存（查询侧由 build_train_multi_segment_payload 写入）
    train_score,
    validate_train_submission,
)
from app.capabilities.train_multi_segment.train_skill_schema import (
    TRAIN_MULTI_SEGMENT_SKILL,
    TrainLeg,
    TrainMultiSegmentPayload,
    TrainSubmitPayload,
    TrainSubmitResult,
)
from app.utils.logger import app_logger

# 刻意不做"同一批段只推一次"的去重：历史消息不携带 skill_ui 帧，用户刷新页面后
# 卡片会消失，若此时去重仍生效则之后每一轮都不再下发 → 交通步变成死路（永远无法提交）。
# 改为每轮都重推：候选走签名缓存 _SIG_CACHE（不会重复打 12306），前端同技能卡片只保留
# 最新一张，因此重推在界面上是幂等的。

# 表单"交通方式"可能是中文选项值，也可能是模型归一后的枚举值，两种都认
_TRAIN_WORDS = {"train", "高铁", "火车", "动车", "高铁/火车"}

# 交通步之后的步骤（用于提交接口的幂等判定：重复提交不再回写状态）
_AFTER_TRANSPORT = {
    "accommodation_planning", "food_planning", "itinerary_generation",
    "budget_summarization", "report_generation",
}


def train_mode_requested(state: dict) -> bool:
    """是否走高铁路线：表单勾了高铁（或已选定 train）才推卡片，航班/自驾保持原流程"""
    if state.get("selected_transport") == "train":
        return True
    req = state.get("user_requirement") or {}
    picked = {str(v).strip() for v in (req.get("transport_types") or [])}
    return bool(picked & _TRAIN_WORDS)


def multi_leg_train(state: dict) -> bool:
    """是否走"高铁由代码供给卡片"的路线：高铁 + 段数≥1（0 段=本地游玩，走零段直通）"""
    return train_mode_requested(state) and len(_legs_with_dates(state)) >= 1


def _legs_with_dates(state: dict) -> list[TrainLeg]:
    """按行程时序生成「段 + 段日期」：去程=出发日、城际=下一城首日（_leg_query_date 口径）"""
    from app.core.middleware import _leg_query_date
    from app.tools.state_transition import _expected_legs
    legs = _expected_legs(state)
    out: list[TrainLeg] = []
    for index, leg in enumerate(legs):
        frm, _, to = leg.partition("→")
        if not frm or not to:
            continue
        out.append(TrainLeg(
            from_=frm, to=to, travel_date=_leg_query_date(state, legs, index),
        ))
    return out


def _fmt_option(opt: Any) -> str:
    """车次 → 段账本文本（与 select_transport_tool 落库口径一致，行程步直接可读）"""
    price = f"{opt.price:g}"
    return f"{opt.trainNo} {opt.startTime}-{opt.endTime} {opt.seatType}{price}元"

async def emit_train_skill_ui(
    stream: Any, conversation_id: str, state: dict
) -> tuple[Optional[TrainMultiSegmentPayload], str]:
    """手动确认模式：并行查全部段 → 推 skill_ui 帧

    返回 (payload, status)，status 供调用方决定正文怎么写：
      pushed    —— 已下发卡片（payload 非空）
      empty     —— 各段都没查到候选，未下发卡片（调用方须如实告知，不能静默）
      skipped   —— 不走高铁卡片路线（未选高铁 / 0 段）
    """
    if not train_mode_requested(state):
        return None, "skipped"
    legs = _legs_with_dates(state)
    if len(legs) < 1:                        # 0 段（出发地=目的地）走零段直通，不推空卡片
        return None, "skipped"
    payload = await _payload_for(conversation_id, legs)
    # ★ 全部段都为空就不推：宁可不推，也不推一张空卡片
    if not any(seg.train_list for seg in payload.segments):
        app_logger.warning("⚠️ 交通步多段班次全部为空，本次不下发选择卡片（避免空卡片）")
        return None, "empty"
    # ★ 一帧 skill_ui：前端按 skill_name 把卡片渲染在聊天流里
    stream.push(make_event(ROLE_SKILL_UI, {
        "skill_name": TRAIN_MULTI_SEGMENT_SKILL,
        "data": payload.model_dump(by_alias=True),
    }))
    app_logger.info(
        f"🚄 已下发多段高铁选择卡片：{len(payload.segments)} 段"
        f"（各段候选 {[len(s.train_list) for s in payload.segments]}）"
    )
    return payload, "pushed"


def build_lead_text(payload: Optional[TrainMultiSegmentPayload]) -> str:
    """卡片上方的一句引导语（确定性文案，不用模型自由发挥）"""
    n = len(payload.segments) if payload else 0
    return (f"已为您**并行查询**到全部 {n} 段高铁的真实班次（段与段同时查询，未做先后校验）。"
            f"请在下方卡片中依次选定每一段车次，确认无误后一次提交。\n")


# 本批段的签名缓存：同一会话、同一批段的后续轮次直接复用，避免同一批段被反复查 12306
_SIG_CACHE: SignatureCache[TrainMultiSegmentPayload] = SignatureCache("train")


def _all_have_options(payload: TrainMultiSegmentPayload) -> bool:
    """高铁的缓存口径：每一段都有候选才缓存（某段偶发为空就不缓存，下次重试会重新查询）"""
    # ★ 高铁口径：每一段都要有候选
    return all(seg.train_list for seg in payload.segments)


async def _payload_for(conversation_id: str, legs: list[TrainLeg]) -> TrainMultiSegmentPayload:
    """同一会话同一批段只查一次：命中完整快照直接复用，否则并行查询，并按高铁口径决定写不写缓存"""
    sig = leg_signature(legs)
    cached = _SIG_CACHE.get(conversation_id, sig, is_complete=_all_have_options)
    if cached is not None:
        return cached
    payload = await build_train_multi_segment_payload(legs, cache_key=conversation_id)
    _SIG_CACHE.store(conversation_id, sig, payload, is_complete=_all_have_options)
    return payload


def _pick_best(options: list[Any]) -> Optional[Any]:
    """自动模式代选：有票优先 → 综合分最低（train_score＝历时+价/10，与快速通道同口径）"""
    if not options:
        return None
    # ★ 只排除"无票"（候补也算可选）；全都无票时退回全体里挑
    avail = [o for o in options if o.ticketStatus != "无票"] or list(options)
    return min(avail, key=lambda o: train_score(duration_to_minutes(o.duration), float(o.price)))


async def auto_select_train_legs(conversation_id: str, state: dict) -> list[str]:
    """自动确认模式：不弹卡片，由代码按「有票 + 耗时 + 价格」代选全部段并推进到住宿步

    返回已选定段的展示文本（空列表 = 未推进，调用方跳过即可）
    """
    legs = _legs_with_dates(state)
    if not legs:
        return []
    payload = await _payload_for(conversation_id, legs)
    selections: dict[str, str] = {}
    prices: dict[str, float] = {}           # ★ 数字账本：与展示文本同一次写入
    texts: list[str] = []
    for index, leg in enumerate(legs):
        seg = payload.segments[index] if index < len(payload.segments) else None
        best = _pick_best(list(seg.train_list) if seg else [])
        if best is None:
            continue                          # 该段无候选：不硬推进，交回原流程如实告知
        key = f"{leg.from_}→{leg.to}"
        selections[key] = _fmt_option(best)
        if float(best.price) > 0:             # ★ 只记真实票价；没有就不记，预算侧自行估算
            prices[key] = float(best.price)
        texts.append(f"{key} {_fmt_option(best)}")
    if len(selections) != len(legs):
        app_logger.warning(
            f"⚠️ 自动模式代选不完整（{len(selections)}/{len(legs)} 段有候选），本轮不推进"
        )
        return []
    agent, config, _ = await get_agent_and_state(conversation_id)
    await agent.aupdate_state(config, {
        "leg_selections": selections,
        "leg_prices": prices,                 # ★ 展示文本给人看，这份给第 11 章的预算工具算
        "leg_cursor": len(legs),
        "selected_transport": "train",
        "selected_transport_legs": texts,
        "current_step": "accommodation_planning",
    })
    app_logger.info(f"✅ 自动模式已代选 {len(selections)} 段高铁（耗时+价格最优），推进到住宿步")
    return texts


async def advance_zero_leg(conversation_id: str) -> bool:
    """ 零段直通——出发地与目的地同城（本地游玩）无城际段，

    代码直接把 current_step 从交通步推进到住宿步（不经模型），避免模型空转/编造班次
    """
    agent, config, _ = await get_agent_and_state(conversation_id)
    try:
        # 零段轮必须同时写入 selected_transport / selected_transport_legs。
        # 住宿步与行程步的 requires 都含 selected_transport，缺字段会在下一轮模型调用时
        # 会话提交住宿卡后即报此错，用户卡死在行程步）。取 "local" 语义最准：确实没有任何城际段
        await agent.aupdate_state(config, {
            "current_step": "accommodation_planning",
            "selected_transport": "local",
            "selected_transport_legs": [],
            # ★ 显式清空：用户回退换过交通方式时，旧票价不能留在状态里
            "leg_prices": {},
        })
        app_logger.info("🚕 零段直通：无城际交通段（出发地=目的地），已由代码推进至住宿步")
        return True
    except Exception as exc:
        app_logger.warning(f"⚠️ 零段直通推进失败（不影响流程继续）：{exc}")
        return False


_LEG_UNAVAILABLE = "实时班次暂不可用（可现场购票/改期）"


async def apply_train_selection(
    conversation_id: str, payload: TrainSubmitPayload
) -> TrainSubmitResult:
    """前端一次性提交 → 校验 + 写段账本 + 推进到住宿步（全程不做列车时间校验）"""
    result = validate_train_submission(payload, cache_key=conversation_id)
    if not result.ok:
        return result
    agent, config, state = await get_agent_and_state(conversation_id)   # ← 复用 base
    # 步骤守卫（代码层，不看模型措辞）：只有仍停在交通步才允许落库推进——
    # 重复提交（前端连点/已确认过）按幂等成功处理，避免把已推进的状态机拉回到住宿步
    step = str(state.get("current_step") or "")
    if step != "transport_planning":
        if step in _AFTER_TRANSPORT:
            return TrainSubmitResult(
                ok=True, code="OK", msg="该批车次已确认，无需重复提交",
                accepted=list(payload.segments),
                texts=list(state.get("selected_transport_legs") or []),
            )
        return TrainSubmitResult(
            ok=False, code="STEP_MISMATCH",
            msg=f"当前流程步骤为 {step or '未知'}，不在交通确认阶段，无法提交车次",
        )
    legs = _legs_with_dates(state)
    cached = get_cached_payload(conversation_id)
    by_id = {seg.segment_id: seg for seg in (cached.segments if cached else [])}
    # 按 option_id 定位选中趟次（车次号在同段可重复：G1308 有终到石家庄/正定机场两趟，
    # 若按 trainNo 取首条会把错误的到达站与票价写进段账本）
    chosen = {item.segment_id: item.option_id for item in payload.segments}
    selections: dict[str, str] = {}
    prices: dict[str, float] = {}
    texts: list[str] = []
    for index, leg in enumerate(legs):
        seg = by_id.get(f"s{index + 1}")
        want = chosen.get(f"s{index + 1}")
        opt = next((o for o in (seg.train_list if seg else []) if o.option_id == want), None)
        key = f"{leg.from_}→{leg.to}"
        if opt is not None:
            selections[key] = _fmt_option(opt)
            if float(opt.price) > 0:
                prices[key] = float(opt.price)
            texts.append(f"{key} {_fmt_option(opt)}")
        elif seg is not None and not seg.train_list:
            # ★ 该段实时无候选（卡片里置灰、无票可选）：占位放行，让用户能走下去；
            #   该段**不写进 prices**，预算侧据此知道要对这一段走估算，而不是当成 0 元
            selections[key] = _LEG_UNAVAILABLE
            texts.append(f"{key} {_LEG_UNAVAILABLE}")
        # else：该段有候选却没选中 —— 不占位（validate 已拦），保持"有票必须选"
    if not legs or len(selections) != len(legs):
        return TrainSubmitResult(
            ok=False, code="INCOMPLETE",
            msg=f"提交的车次未能覆盖全部行程段（应 {len(legs)} 段，匹配 {len(selections)} 段），"
                f"请在卡片里重新选择后提交",
        )
    await agent.aupdate_state(config, {
        "leg_selections": selections,
        "leg_prices": prices,
        "leg_cursor": len(legs),
        "selected_transport": "train",
        "selected_transport_legs": texts,
        "current_step": "accommodation_planning",
    })
    app_logger.info(f"✅ 多段高铁已提交并落库：{len(selections)} 段，推进到住宿步")
    return TrainSubmitResult(
        ok=True, code="OK",
        msg=f"已确认 {len(selections)} 段高铁车次，继续为您规划住宿与行程",
        accepted=list(payload.segments),
        texts=texts,  # 段文本回传：路由侧据此合成"我已选定车次…"发言续跑住宿步
    )

__all__ = [
    "train_mode_requested",
    "multi_leg_train",
    "emit_train_skill_ui",
    "auto_select_train_legs",
    "apply_train_selection",
    "build_lead_text",
]