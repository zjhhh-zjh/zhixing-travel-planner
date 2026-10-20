"""多段航班 UI Skill —— 与状态机「交通规划步」的接线层

与多段高铁（train_flow）完全同构，仅数据源换成航班：
  1. emit_flight_skill_ui   手动确认模式：并行查询全部段 → 推 skill_ui 帧（前端内嵌卡片）
  2. auto_select_flights    自动确认模式：代码按「价格低」代选并推进（不弹 UI）
  3. apply_flight_selection 前端提交 → 基础合法性校验（**不做任何时间校验**）→ 写段账本 → 推进

优先级：高铁 > 航班（同时勾选高铁与航班时走高铁卡片，本模块 flight_card_mode 会返回 False）。
"""
from typing import Any, Optional

from app.core.events import ROLE_SKILL_UI, make_event
from app.capabilities.base import SignatureCache, get_agent_and_state, leg_signature
from app.capabilities.flight_multi_segment.flight_agent_tool import (
    build_flight_multi_segment_payload,
    get_cached_payload,
    validate_flight_submission,
)
from app.capabilities.flight_multi_segment.flight_skill_schema import (
    FLIGHT_MULTI_SEGMENT_SKILL,
    FlightLeg,
    FlightMultiSegmentPayload,
    FlightSubmitPayload,
    FlightSubmitResult,
)
from app.capabilities.train_multi_segment.train_flow import train_mode_requested
from app.utils.logger import app_logger

# 表单"交通方式"可能是中文选项值，也可能是模型归一后的枚举值，两种都认
_FLIGHT_WORDS = {"flight", "航班", "飞机", "航班/飞机", "飞机/航班"}

# 交通步之后的步骤（用于提交接口的幂等判定：重复提交不再回写状态）
_AFTER_TRANSPORT = {
    "accommodation_planning", "food_planning", "itinerary_generation",
    "budget_summarization", "report_generation",
}


def flight_mode_requested(state: dict) -> bool:
    """是否走航班路线：表单勾了航班（或已选定 flight）才推卡片"""
    if state.get("selected_transport") == "flight":
        return True
    req = state.get("user_requirement") or {}
    picked = {str(v).strip() for v in (req.get("transport_types") or [])}
    return bool(picked & _FLIGHT_WORDS)


def _legs_with_dates(state: dict) -> list[FlightLeg]:
    """按行程时序生成「段 + 段日期」：去程=出发日、城际=下一城首日（_leg_query_date 口径）"""
    from app.core.middleware import _leg_query_date
    from app.tools.state_transition import _expected_legs
    legs = _expected_legs(state)
    out: list[FlightLeg] = []
    for index, leg in enumerate(legs):
        frm, _, to = leg.partition("→")
        if not frm or not to:
            continue
        out.append(FlightLeg(
            from_=frm, to=to, travel_date=_leg_query_date(state, legs, index),
        ))
    return out


def flight_card_mode(state: dict) -> bool:
    """是否走"航班由代码供给卡片"的路线：选了航班 **且不是高铁路线**（高铁优先）且段数≥1"""
    return (
        flight_mode_requested(state)
        # ★ 高铁优先：两个都勾了，走高铁卡片
        and not train_mode_requested(state)
        and len(_legs_with_dates(state)) >= 1
    )

def flight_unreachable_cities(state: dict) -> list[str]:
    """航班结构性不可查时返回拖累的城市（按出现顺序去重）；只要有一段可查就返回空列表
    判据：每一段都至少有一端不在 _CITY_AIRPORT（如苏州本身无民用机场），此时 _query_one_leg
    会逐段"无机场码，跳过查询"，payload必然全空，重试不会生效。
    区分结构性不可查与数据源瞬时失败：前者直接降级说明，后者提示稍后重试。
    只要存在一段两端都有机场码，仍走卡片流程，不可查段在卡片置灰占位放行。
    """
    from app.tools.mcp_tools import _CITY_AIRPORT
    legs = _legs_with_dates(state)
    if not legs or any(leg.from_ in _CITY_AIRPORT and leg.to in _CITY_AIRPORT for leg in legs):
        return []
    out: list[str] = []
    for leg in legs:
        for city in (leg.from_, leg.to):
            if city not in _CITY_AIRPORT and city not in out:
                out.append(city)
    return out
def _fmt_option(opt: Any) -> str:
    """航班 → 段账本文本（与 select_transport_tool 落库口径一致，行程步直接可读）

    ★ 票价必须写进这串文本：预算侧 _legs_price_per_person() 是按 `(\\d+)元` 正则
      从段账本里抠交通费的（高铁 _fmt_option 末尾就带"二等座553元"）。航班这边原先
      一个"元"字都没有 → 正则空手而归 → 交通费恒按 500 元/人兜底估算，
      用户选了航班就永远拿不到真实交通预算。取不到票价时不写，宁缺毋造。
    """
    base = f"{opt.flight_no} {opt.dep_time}-{opt.arr_time} {opt.dep_airport}→{opt.arr_airport}"
    price = float(getattr(opt, "price", 0) or 0)
    return f"{base} {price:g}元" if price > 0 else base


async def emit_flight_skill_ui(
    stream: Any, conversation_id: str, state: dict
) -> tuple[Optional[FlightMultiSegmentPayload], str]:
    """手动确认模式：并行查全部段 → 推 skill_ui 帧

    返回 (payload, status)，status 供调用方决定正文怎么写：
      pushed    —— 已下发卡片（payload 非空）
      empty     —— 各段都没查到候选，未下发卡片（调用方须如实告知，不能静默）
      skipped   —— 不走航班卡片路线（未选航班 / 同时选了高铁 / 0 段）
    """
    if not flight_card_mode(state):
        return None, "skipped"
    legs = _legs_with_dates(state)
    payload = await _payload_for(conversation_id, legs)
    if not any(seg.flight_list for seg in payload.segments):
        app_logger.warning("⚠️ 交通步多段航班全部为空，本次不下发选择卡片（避免空卡片）")
        return None, "empty"
    stream.push(make_event(ROLE_SKILL_UI, {
        "skill_name": FLIGHT_MULTI_SEGMENT_SKILL,
        "data": payload.model_dump(by_alias=True),
    }))
    app_logger.info(
        f"✈️ 已下发多段航班选择卡片：{len(payload.segments)} 段"
        f"（各段候选 {[len(s.flight_list) for s in payload.segments]}）"
    )
    return payload, "pushed"


def build_lead_text(payload: Optional[FlightMultiSegmentPayload]) -> str:
    """卡片上方的一句引导语（确定性文案，不用模型自由发挥）"""
    n = len(payload.segments) if payload else 0
    text = (f"已为您**并行查询**到全部 {n} 段航班的实时班次（段与段同时查询，未做先后校验）。"
            f"请在下方卡片中依次选定每一段航班，确认无误后一次提交。\n")
    # ★ 票价说明归代码：有价就说有价，没价就把"为什么没价"讲清楚，
    #   免得模型自由发挥去编一个价，或者含糊其辞让用户以为是免费的
    segs = list(payload.segments) if payload else []
    priced = sum(1 for s in segs for o in s.flight_list if o.price > 0)
    total = sum(len(s.flight_list) for s in segs)
    if total and priced == 0:
        text += "（本次票价数据源暂未返回报价，卡片内票价显示为「待定」，请以航司官方渠道为准。）\n"
    elif total and priced < total:
        text += f"（{total} 班中 {priced} 班取到实时票价，其余显示「待定」。）\n"
    return text


# 本批段的签名缓存：同一会话、同一批段的后续轮次直接复用，避免同一批段被反复查航班 MCP
_SIG_CACHE: SignatureCache[FlightMultiSegmentPayload] = SignatureCache("flight")


def _any_has_options(payload: FlightMultiSegmentPayload) -> bool:
    """航班的缓存口径：只要有一段查到航班就缓存（没有机场码的段本来就是空的，重试也查不出来）"""
    # ★ 航班口径：有一段查到就行
    return any(seg.flight_list for seg in payload.segments)


async def _payload_for(conversation_id: str, legs: list[FlightLeg]) -> FlightMultiSegmentPayload:
    """同一会话同一批段只查一次；全部段都为空时不写缓存，留给下次重试"""
    sig = leg_signature(legs)
    cached = _SIG_CACHE.get(conversation_id, sig)
    if cached is not None:
        return cached
    payload = await build_flight_multi_segment_payload(legs, cache_key=conversation_id)
    _SIG_CACHE.store(conversation_id, sig, payload, is_complete=_any_has_options)
    return payload

def _pick_best(options: list[Any]) -> Optional[Any]:
    """自动模式代选：价格更低优先（价格未知=0 视为不可用，排在已知价格之后）"""
    if not options:
        return None
    # ★ 价格未知（0）排到最后；全都未知时 min 取第一班（最早起飞）
    return min(options, key=lambda o: float(o.price) if float(o.price) > 0 else float("inf"))


async def auto_select_flights(conversation_id: str, state: dict) -> list[str]:
    """自动确认模式：不弹卡片，由代码按「价格低」代选全部段并推进到住宿步

    返回已选定段的展示文本（空列表 = 未推进，调用方跳过即可）
    """
    legs = _legs_with_dates(state)
    if not legs:
        return []
    payload = await _payload_for(conversation_id, legs)
    selections: dict[str, str] = {}
    prices: dict[str, float] = {}
    texts: list[str] = []
    for index, leg in enumerate(legs):
        seg = payload.segments[index] if index < len(payload.segments) else None
        best = _pick_best(list(seg.flight_list) if seg else [])
        if best is None:
            continue                          # 该段无候选：不硬推进，交回原流程如实告知
        key = f"{leg.from_}→{leg.to}"
        selections[key] = _fmt_option(best)
        # ★ 航班票价常常查不到（时刻表接口不返回价）：查不到就不写，别拿 0 冒充真实价
        if float(getattr(best, "price", 0) or 0) > 0:
            prices[key] = float(best.price)
        texts.append(f"{key} {_fmt_option(best)}")
    if len(selections) != len(legs):
        app_logger.warning(
            f"⚠️ 自动模式航班代选不完整（{len(selections)}/{len(legs)} 段有候选），本轮不推进"
        )
        return []
    agent, config, _ = await get_agent_and_state(conversation_id)
    await agent.aupdate_state(config, {
        "leg_selections": selections,
        "leg_prices": prices,
        "leg_cursor": len(legs),
        "selected_transport": "flight",
        "selected_transport_legs": texts,
        "current_step": "accommodation_planning",
    })
    app_logger.info(f"✅ 自动模式已代选 {len(selections)} 段航班（价格最优），推进到住宿步")
    return texts

_LEG_UNAVAILABLE = "实时班次暂不可用（可现场购票/改期）"


async def apply_flight_selection(
    conversation_id: str, payload: FlightSubmitPayload
) -> FlightSubmitResult:
    """前端一次性提交 → 校验 + 写段账本 + 推进到住宿步（**全程不做航班时间校验**）"""
    result = validate_flight_submission(payload, cache_key=conversation_id)
    if not result.ok:
        return result
    agent, config, state = await get_agent_and_state(conversation_id)
    # 步骤守卫（代码层，不看模型措辞）：只有仍停在交通步才允许落库推进——
    # 重复提交（前端连点/已确认过）按幂等成功处理，避免把已推进的状态机拉回来
    step = str(state.get("current_step") or "")
    if step != "transport_planning":
        if step in _AFTER_TRANSPORT:
            return FlightSubmitResult(
                ok=True, code="OK", msg="该批航班已确认，无需重复提交",
                accepted=list(payload.segments),
                texts=list(state.get("selected_transport_legs") or []),
            )
        return FlightSubmitResult(
            ok=False, code="STEP_MISMATCH",
            msg=f"当前流程步骤为 {step or '未知'}，不在交通确认阶段，无法提交航班",
        )
    legs = _legs_with_dates(state)
    cached = get_cached_payload(conversation_id)
    by_id = {seg.segment_id: seg for seg in (cached.segments if cached else [])}
    # 按 option_id 定位选中航班（航班号可能不唯一，不能按 flight_no 取首条）
    chosen = {item.segment_id: item.option_id for item in payload.segments}
    selections: dict[str, str] = {}
    prices: dict[str, float] = {}
    texts: list[str] = []
    for index, leg in enumerate(legs):
        seg = by_id.get(f"s{index + 1}")
        want = chosen.get(f"s{index + 1}")
        opt = next((o for o in (seg.flight_list if seg else []) if o.option_id == want), None)
        key = f"{leg.from_}→{leg.to}"
        if opt is not None:
            selections[key] = _fmt_option(opt)
            if float(getattr(opt, "price", 0) or 0) > 0:
                prices[key] = float(opt.price)
            texts.append(f"{key} {_fmt_option(opt)}")
        elif seg is not None and not seg.flight_list:
            # ★ 该段实时无候选（卡片里置灰、无从选择）：占位放行（与高铁/自驾同口径），
            #   预算侧对无价段按估算兜底，不再卡死在交通步
            selections[key] = _LEG_UNAVAILABLE
            texts.append(f"{key} {_LEG_UNAVAILABLE}")
        # else：该段有候选却没选中 —— 不占位（validate 已拦），保持"有票必须选"
    if not legs or len(selections) != len(legs):
        return FlightSubmitResult(
            ok=False, code="INCOMPLETE",
            msg=f"提交的航班未能覆盖全部行程段（应 {len(legs)} 段，匹配 {len(selections)} 段），"
                f"请在卡片里重新选择后提交",
        )
    await agent.aupdate_state(config, {
        "leg_selections": selections,
        "leg_prices": prices,
        "leg_cursor": len(legs),
        "selected_transport": "flight",
        "selected_transport_legs": texts,
        "current_step": "accommodation_planning",
    })
    app_logger.info(f"✅ 多段航班已提交并落库：{len(selections)} 段，推进到住宿步")
    return FlightSubmitResult(
        ok=True, code="OK",
        msg=f"已确认 {len(selections)} 段航班，继续为您规划住宿与行程",
        accepted=list(payload.segments),
        texts=texts,  # 段文本回传：路由侧据此合成"我已选定航班…"发言续跑住宿步
    )



__all__ = [
    "flight_mode_requested",
    "flight_card_mode",
    "emit_flight_skill_ui",
    "auto_select_flights",
    "apply_flight_selection",
    "build_lead_text",
]