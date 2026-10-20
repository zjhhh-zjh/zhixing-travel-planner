"""多段自驾 UI Skill —— 与状态机「交通规划步」的接线层

与多段航班（flight_flow）完全同构，仅数据源换成高德驾车路线，且**只读确认**：
  1. emit_driving_skill_ui    手动确认模式：并行查询全部段 → 推 skill_ui 帧（前端内嵌卡片）
  2. auto_select_driving      自动确认模式：无需选择，路线查到即直接落库推进（不弹 UI）
  3. apply_driving_selection  前端提交 → 基础合法性校验（**不做任何时间/路线校验**）→ 写段账本 → 推进

优先级：高铁 > 航班 > 自驾（同时勾选多种方式时，靠前者接管，本模块 driving_card_mode 返回 False）。
与航班/高铁的唯一差异：每段只给一条默认策略路线，用户不能选策略，只能整批【确认】。
"""
from typing import Any, Optional

from app.core.events import ROLE_SKILL_UI, make_event
from app.capabilities.base import SignatureCache, get_agent_and_state, leg_signature
from app.capabilities.driving_multi_segment.driving_agent_tool import (
    build_driving_multi_segment_payload,
    get_cached_payload,
    validate_driving_submission,
)
from app.capabilities.driving_multi_segment.driving_skill_schema import (
    DRIVING_MULTI_SEGMENT_SKILL,
    DrivingLeg,
    DrivingMultiSegmentPayload,
    DrivingSegment,
    DrivingSubmitPayload,
    DrivingSubmitResult,
)
from app.utils.logger import app_logger

# 表单"交通方式"可能是中文选项值，也可能是模型归一后的枚举值，两种都认
_DRIVING_WORDS = {"driving", "自驾", "开车", "驾车", "自驾/开车"}

# 交通步之后的步骤（用于提交接口的幂等判定：重复提交不再回写状态）
_AFTER_TRANSPORT = {
    "accommodation_planning", "food_planning", "itinerary_generation",
    "budget_summarization", "report_generation",
}


def driving_mode_requested(state: dict) -> bool:
    """是否走自驾路线：表单勾了自驾（或已选定 driving）才推卡片"""
    if state.get("selected_transport") == "driving":
        return True
    req = state.get("user_requirement") or {}
    picked = {str(v).strip() for v in (req.get("transport_types") or [])}
    return bool(picked & _DRIVING_WORDS)


def _legs_with_dates(state: dict) -> list[DrivingLeg]:
    """按行程时序生成「段 + 段日期」：去程=出发日、城际=下一城首日（_leg_query_date 口径）

    自驾不依赖发车时刻，日期仅作展示；解析不出时留空（具体由 _leg_query_date 决定）。
    """
    from app.core.middleware import _leg_query_date
    from app.tools.state_transition import _expected_legs
    legs = _expected_legs(state)
    out: list[DrivingLeg] = []
    for index, leg in enumerate(legs):
        frm, _, to = leg.partition("→")
        if not frm or not to:
            continue
        out.append(DrivingLeg(
            from_=frm, to=to, travel_date=_leg_query_date(state, legs, index),
        ))
    return out


def driving_card_mode(state: dict) -> bool:
    """是否走"自驾由代码供给卡片"的路线：选了自驾 **且不是高铁/航班路线**（优先级靠后）且段数≥1"""
    from app.capabilities.flight_multi_segment.flight_flow import flight_mode_requested
    from app.capabilities.train_multi_segment.train_flow import train_mode_requested
    return (
        driving_mode_requested(state)
        and not train_mode_requested(state)     # 优先级：高铁 > 航班 > 自驾
        # ★ 优先级最低：高铁、航班都没勾才轮到自驾
        and not flight_mode_requested(state)
        and len(_legs_with_dates(state)) >= 1
    )


def _fmt_route(seg: DrivingSegment) -> str:
    """路线 → 段账本文本（无班次号，故用"自驾 距离 耗时"口径，行程步直接可读）"""
    dist = f"{seg.distance_km:g}km" if seg.distance_km > 0 else "距离未知"
    dur = seg.duration_text or "耗时未知"
    return f"自驾 {dist} {dur}"


async def emit_driving_skill_ui(
    stream: Any, conversation_id: str, state: dict
) -> tuple[Optional[DrivingMultiSegmentPayload], str]:
    """手动确认模式：并行查全部段 → 推 skill_ui 帧

    返回 (payload, status)，status 供调用方决定正文怎么写：
      pushed    —— 已下发卡片（payload 非空）
      empty     —— 各段都没查到路线，未下发卡片（调用方须如实告知，不能静默）
      skipped   —— 不走自驾卡片路线（未选自驾 / 选了高铁或航班 / 0 段）
    """
    if not driving_card_mode(state):
        return None, "skipped"
    legs = _legs_with_dates(state)
    payload = await _payload_for(conversation_id, legs)
    if not any(seg.ok for seg in payload.segments):
        app_logger.warning("⚠️ 交通步多段自驾路线全部为空，本次不下发确认卡片（避免空卡片）")
        return None, "empty"
    stream.push(make_event(ROLE_SKILL_UI, {
        "skill_name": DRIVING_MULTI_SEGMENT_SKILL,
        "data": payload.model_dump(by_alias=True),
    }))
    app_logger.info(
        f"🚗 已下发多段自驾确认卡片：{len(payload.segments)} 段"
        f"（各段 ok={[s.ok for s in payload.segments]}）"
    )
    return payload, "pushed"


def build_lead_text(payload: Optional[DrivingMultiSegmentPayload]) -> str:
    """卡片上方的一句引导语（确定性文案，不用模型自由发挥）"""
    n = len(payload.segments) if payload else 0
    return (f"已为您**并行查询**到全部 {n} 段自驾路线（每段均为高德默认策略的最优路线，"
            f"不可选策略）。请在下方卡片中逐段核对，确认无误后一次提交。\n")


# 本批段的签名缓存：同一会话、同一批段的后续轮次直接复用，避免同一批段被反复查高德 MCP
_SIG_CACHE: SignatureCache[DrivingMultiSegmentPayload] = SignatureCache("driving")


def _any_ok(payload: DrivingMultiSegmentPayload) -> bool:
    """自驾的缓存口径：只要有一段查到路线就缓存"""
    # ★ 自驾口径：有一段查到路线就行
    return any(seg.ok for seg in payload.segments)


async def _payload_for(
    conversation_id: str, legs: list[DrivingLeg]
) -> DrivingMultiSegmentPayload:
    """同一会话同一批段只查一次；全部段都失败时不写缓存，留给下次重试"""
    sig = leg_signature(legs)
    cached = _SIG_CACHE.get(conversation_id, sig)
    if cached is not None:
        return cached
    payload = await build_driving_multi_segment_payload(legs, cache_key=conversation_id)
    _SIG_CACHE.store(conversation_id, sig, payload, is_complete=_any_ok)
    return payload

async def auto_select_driving(conversation_id: str, state: dict) -> list[str]:
    """自动确认模式：无需选择，路线全部查到即直接落库推进到住宿步

    返回已确认段的展示文本（空列表 = 未推进，调用方跳过即可）
    """
    legs = _legs_with_dates(state)
    if not legs:
        return []
    payload = await _payload_for(conversation_id, legs)
    selections: dict[str, str] = {}
    texts: list[str] = []
    for index, leg in enumerate(legs):
        seg = payload.segments[index] if index < len(payload.segments) else None
        # ★ 自驾不用选：每段都查到了才推进，任何一段失败就不推进
        if seg is None or not seg.ok:
            continue                          # 该段无路线：不硬推进，交回原流程如实告知
        key = f"{leg.from_}→{leg.to}"
        selections[key] = _fmt_route(seg)
        texts.append(f"{key} {_fmt_route(seg)}")
    if len(selections) != len(legs):
        app_logger.warning(
            f"⚠️ 自动模式自驾路线不完整（{len(selections)}/{len(legs)} 段可查），本轮不推进"
        )
        return []
    agent, config, _ = await get_agent_and_state(conversation_id)
    await agent.aupdate_state(config, {
        "leg_selections": selections,
        # ★ 自驾没有票价可记，但必须把数字账本**显式清空**：用户如果是从高铁/航班
        #   回退过来改成自驾的，上一次的票价还留在状态里，预算会算出一笔不存在的车票钱
        "leg_prices": {},
        "leg_cursor": len(legs),
        "selected_transport": "driving",
        "selected_transport_legs": texts,
        "current_step": "accommodation_planning",
    })
    app_logger.info(f"✅ 自动模式已确认 {len(selections)} 段自驾路线（默认策略），推进到住宿步")
    return texts


_LEG_UNAVAILABLE = "实时班次暂不可用（可现场购票/改期）"


async def apply_driving_selection(
        conversation_id: str, payload: DrivingSubmitPayload
) -> DrivingSubmitResult:
    """前端一次性提交 → 校验 + 写段账本 + 推进到住宿步（全程只读确认，不做任何校验）"""
    result = validate_driving_submission(payload, cache_key=conversation_id)
    if not result.ok:
        return result
    agent, config, state = await get_agent_and_state(conversation_id)  # ← 复用 base
    # 步骤守卫（代码层，不看模型措辞）：只有仍停在交通步才允许落库推进——
    # 重复提交（前端连点/已确认过）按幂等成功处理，避免把已推进的状态机拉回来
    step = str(state.get("current_step") or "")
    if step != "transport_planning":
        if step in _AFTER_TRANSPORT:
            return DrivingSubmitResult(
                ok=True, code="OK", msg="该批自驾路线已确认，无需重复提交",
                accepted=list(payload.segments),
                texts=list(state.get("selected_transport_legs") or []),
            )
        return DrivingSubmitResult(
            ok=False, code="STEP_MISMATCH",
            msg=f"当前流程步骤为 {step or '未知'}，不在交通确认阶段，无法提交自驾路线",
        )
    legs = _legs_with_dates(state)
    cached = get_cached_payload(conversation_id)
    by_id = {seg.segment_id: seg for seg in (cached.segments if cached else [])}
    selections: dict[str, str] = {}
    texts: list[str] = []
    for index, leg in enumerate(legs):
        seg = by_id.get(f"s{index + 1}")
        key = f"{leg.from_}→{leg.to}"
        if seg is not None and seg.ok:
            selections[key] = _fmt_route(seg)
            texts.append(f"{key} {_fmt_route(seg)}")
        elif seg is not None and not seg.ok:
            # ★ 该段实时无路线（高德查询失败、卡片里置灰）：占位放行，让用户能走下去
            selections[key] = _LEG_UNAVAILABLE
            texts.append(f"{key} {_LEG_UNAVAILABLE}")
        # else：seg 不存在（理论不会）——跳过
    if not legs or len(selections) != len(legs):
        return DrivingSubmitResult(
            ok=False, code="INCOMPLETE",
            msg=f"提交的自驾路线未能覆盖全部行程段（应 {len(legs)} 段，可确认 {len(selections)} 段），"
                f"请稍后在卡片里重新确认后提交",
        )
    await agent.aupdate_state(config, {
        "leg_selections": selections,
        # ★ 自驾没有票价可记，但必须把数字账本**显式清空**：用户如果是从高铁/航班
        #   回退过来改成自驾的，上一次的票价还留在状态里，预算会算出一笔不存在的车票钱
        "leg_prices": {},
        "leg_cursor": len(legs),
        "selected_transport": "driving",
        "selected_transport_legs": texts,
        "current_step": "accommodation_planning",
    })
    app_logger.info(f"✅ 多段自驾已提交并落库：{len(selections)} 段，推进到住宿步")
    return DrivingSubmitResult(
        ok=True, code="OK",
        msg=f"已确认 {len(selections)} 段自驾路线，继续为您规划住宿与行程",
        accepted=list(payload.segments),
        texts=texts,  # 段文本回传：路由侧据此合成"我已确认自驾…"发言续跑住宿步
    )


__all__ = [
    "driving_mode_requested",
    "driving_card_mode",
    "emit_driving_skill_ui",
    "auto_select_driving",
    "apply_driving_selection",
    "build_lead_text",
]