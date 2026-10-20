"""多段住宿 UI Skill —— 与状态机「住宿规划步」的接线层

背景：住宿步原本是**逐城串行**流程（prompt 驱动：一次只查一城、只展示一城、用户确认一城），
账本由 select_accommodation_tool 的 stay_cursor/selected_hotels 维护，多城行程要来回 N 轮。
本模块把这段循环换成「代码一次并行查全部站 → 前端卡片一次选完 → 一次提交」，
与 train_multi_segment（多段高铁）完全同构：

  1. emit_hotel_skill_ui   手动确认模式：按行程各站并行查询 → 推 skill_ui 帧（前端内嵌卡片）
  2. auto_select_hotels    自动确认模式：代码代选,并推进（不弹 UI）
  3. apply_hotel_selection 前端提交 → 基础合法性校验（**不做任何日期校验**）→ 写账本 → 推进

段口径（与高铁分段同源）：段 = 行程目的地**逐站**；入住 = 该城首日、离店 = 该城末日次日（退房日）。
与 middleware._leg_query_date 完全一致，
保证"住宿段"与"交通段"的日期时序不会互相矛盾。
"""
from typing import Any, Optional

from app.core.events import ROLE_SKILL_UI, make_event
from app.capabilities.base import SignatureCache, get_agent_and_state
from app.capabilities.hotel_multi_segment.hotel_agent_tool import (
    build_hotel_multi_segment_payload,
    get_cached_payload,
    pick_best_hotel,
    validate_hotel_multi_submission,
)
from app.capabilities.hotel_multi_segment.hotel_skill_schema import (
    HOTEL_MULTI_SEGMENT_SKILL,
    HotelMultiSegmentPayload,
    HotelMultiSegmentSubmitPayload,
    HotelMultiSegmentSubmitResult,
    HotelSegmentInput,
)
from app.utils.logger import app_logger

# 住宿步之后的步骤（提交接口的幂等判定：重复提交不再回写状态，避免把状态机拽回住宿步）
_AFTER_STAY = {
    "food_planning", "itinerary_generation", "budget_summarization", "report_generation",
}

# 表单住宿档位（枚举） → 展示与检索用中文档位（hotel_parser._level_of 按中文关键字判档）
_LEVEL_LABELS = {
    "star_hotel": "星级酒店", "economy_hotel": "经济型",
    "hostel": "民宿", "youth_hostel": "青年旅社",
}


def _stay_cities(state: dict) -> list[str]:
    """住宿城市序列 = 目的地游玩城市序列（**出发地仅当本身也在目的地串里才计入**）

 （用户规格）：不再无条件把出发地前置——北京→上海 = [上海] 一站；
    北京→天津/上海 = [天津,上海] 两站；北京→北京 = [北京] 一站。
    与快速通道 _plan_cities / 段日期 ordered_play_cities 完全同源
    """
    from app.services.express_planning_service import ordered_play_cities
    req = state.get("user_requirement") or {}
    dest = str(state.get("selected_destination") or "")
    if not dest:
        dest = str(req.get("destination") or "")
    return ordered_play_cities(str(req.get("departure_city") or ""), dest)


def _stay_windows(state: dict) -> list[tuple[str, str, str]]:
    """每站 (城市, 入住日, 离店日)：首站 1 晚 + 剩余天数在其后城市均分（首站 = 出发日入住）

    日期全部由代码算（模型不参与）：天数缺失/出发日非法 → 返回空列表，调用方退回原流程。
 与 middleware._leg_query_date 同源——离店日 = 下一段的乘车/转场日，
    末站离店日 = 末日（返程当天），总晚数 N-1 与预算口径一致；末站当天到当天走则不订住宿
    """
    from datetime import date as _date, timedelta as _timedelta
    from app.services.express_planning_service import _stay_nights
    cities = _stay_cities(state)
    req = state.get("user_requirement") or {}
    days = int(req.get("travel_days") or 0)
    try:
        start = _date.fromisoformat(str(req.get("departure_date") or "")[:10])
    except Exception:
        return []
    if not cities or days <= 0:
        return []
    # ★ 与段日期同一口径：首站 1 晚，其余均分
    nights = _stay_nights(days, len(cities))
    if not nights:
        return []
    out: list[tuple[str, str, str]] = []
    offset = 0
    # ★ 末站离店日封顶到末日：返程当天退房，N 天住 N-1 晚
    last_out = max(days - 1, 1)
    for city, night in zip(cities, nights):
        cin, cout = offset, min(offset + night, last_out)
        if cout > cin:  # 末站当天到、当天返程：不需要住宿
            out.append((
                city,
                (start + _timedelta(days=cin)).isoformat(),
                (start + _timedelta(days=cout)).isoformat(),
            ))
        offset += night
    return out


def _segments(state: dict) -> list[HotelSegmentInput]:
    """组装 Agent Skill 入参（段数动态 = 行程城市数；人数/档位取自表单，缺省兜底）"""
    req = state.get("user_requirement") or {}
    guest = max(1, int(req.get("adult_count") or 1) + int(req.get("children_count") or 0))
    level = _LEVEL_LABELS.get(
        str((list(req.get("accommodation_types") or []) or [""])[0]), "经济型"
    )
    return [
        HotelSegmentInput(
            city=city, checkInDate=check_in, checkOutDate=check_out,
            guestCount=guest, priceLevel=level,
        )
        for city, check_in, check_out in _stay_windows(state)
    ]


def _accom_types(state: dict) -> list[str]:
    """表单预选住宿档位（枚举列表，行程步据此安排住宿；未预选返回空列表）"""
    valid = {"star_hotel", "economy_hotel", "hostel", "youth_hostel"}
    return [t for t in ((state.get("user_requirement") or {}).get("accommodation_types") or [])
            if t in valid]


def hotel_card_mode(state: dict) -> bool:
    """是否走"卡片一次性选择"路线：有城市且有合法出发日/天数才走，否则退回原逐城流程"""
    return bool(_stay_windows(state))


# 本批住宿段的签名缓存：同一会话、同一批住宿的后续轮次直接复用，避免被反复查 MCP
_SIG_CACHE: SignatureCache[HotelMultiSegmentPayload] = SignatureCache("hotel")


def _any_has_candidates(payload: HotelMultiSegmentPayload) -> bool:
    """住宿的缓存口径：只要有一站有候选（门店或品牌）就缓存"""
    # ★ 住宿口径：有一站有候选就行
    return any(seg.hotel_list or seg.brand_list for seg in payload.segments)


def _hotel_signature(segments: list[HotelSegmentInput]) -> str:
    """住宿段没有 from_/to，签名改用"城市@入住日~离店日"拼接"""
    return "|".join(f"{s.city}@{s.checkInDate}~{s.checkOutDate}" for s in segments)


async def _payload_for(
    conversation_id: str, segments: list[HotelSegmentInput]
) -> HotelMultiSegmentPayload:
    """同一会话同一批住宿只查一次；各站候选全为空时不写缓存，留给下次重试"""
    sig = _hotel_signature(segments)
    cached = _SIG_CACHE.get(conversation_id, sig)
    if cached is not None:
        return cached
    payload = await build_hotel_multi_segment_payload(segments, cache_key=conversation_id)
    _SIG_CACHE.store(conversation_id, sig, payload, is_complete=_any_has_candidates)
    return payload


async def emit_hotel_skill_ui(
    stream: Any, conversation_id: str, state: dict
) -> tuple[Optional[HotelMultiSegmentPayload], str]:
    """手动确认模式：并行查全部站 → 推 skill_ui 帧

    返回 (payload, status)，status 供调用方决定正文怎么写：
      pushed    —— 已下发卡片（payload 非空）
      empty     —— 各站都没查到任何候选，未下发卡片（调用方须如实告知，不能静默）
      skipped   —— 缺城市/日期不合法（不接管，交回原逐城流程）
    """
    segments = _segments(state)
    if not segments:
        return None, "skipped"
    payload = await _payload_for(conversation_id, segments)
    if not any(seg.hotel_list or seg.brand_list for seg in payload.segments):
        app_logger.warning("⚠️ 住宿步各站候选全为空，本次不下发选择卡片（避免空卡片）")
        return None, "empty"
    stream.push(make_event(ROLE_SKILL_UI, {
        "skill_name": HOTEL_MULTI_SEGMENT_SKILL,
        "data": payload.model_dump(by_alias=True),
    }))
    app_logger.info(
        f"🏨 已下发多段住宿选择卡片：{len(payload.segments)} 站"
        f"（模式 {[f'{s.segment_id}={s.mode}' for s in payload.segments]}）"
    )
    return payload, "pushed"


def build_lead_text(payload: Optional[HotelMultiSegmentPayload]) -> str:
    """卡片上方引导语（确定性文案）：**逐站写明城市与入住日期**，避免用户不知道每家酒店在哪"""
    if not payload or not payload.segments:
        return ""
    lines = []
    for index, seg in enumerate(payload.segments, 1):
        note = "门店候选" if seg.mode == "hotel" else "品牌候选，该城市实时报价暂不可用"
        lines.append(
            f"- 第 {index} 站 {seg.city}：{seg.checkInDate} 入住 · {seg.checkOutDate} 离店（{note}）"
        )
    return (
        f"已为您**并行查询**全部 {len(payload.segments)} 站的住宿候选：\n"
        + "\n".join(lines)
        + "\n请在下方卡片中**按城市逐站**选择（每站卡片顶部都标注了所在城市与入住日期），"
          "全部选完后在预览页一次性提交。\n"
    )


def _next_step(state: dict) -> tuple[dict, str]:
    """住宿确认完成后的落点：**直接进行程生成**（不再插入餐饮确认轮）

    用户口径：餐饮偏好是表单必填项，住宿（最后一个需人工选择的项）确认后应直接出行程报告。
    手动/自动模式一致；表单异常缺值时按"特色美食"兜底——不能因为一个空值把流程卡在餐饮轮，
    那样用户会觉得"选完酒店就没反应了"（实测确实卡住过）。
    """
    prefs = [str(v) for v in ((state.get("user_requirement") or {}).get("food_preferences") or [])]
    return {"selected_food_types": prefs or ["specialty"]}, "itinerary_generation"


async def auto_select_hotels(conversation_id: str, state: dict) -> list[str]:
    """自动确认模式：不弹卡片，由代码代选全部站并推进（门店取首条 / 品牌取首个）

    返回已选定站的展示文本（空列表 = 未推进，调用方跳过即可）
    """
    segments = _segments(state)
    if not segments:
        return []
    payload = await _payload_for(conversation_id, segments)
    texts: list[str] = []
    prices: dict[str, float] = {}
    for seg in payload.segments:
        head = f"{seg.city}（{seg.checkInDate} 至 {seg.checkOutDate}）"
        if seg.hotel_list:
            # ★ 门店择优与快速通道同源（同一批候选）：评分≥4.5 取低价、否则取评分最高，
            #   异常低价剔除；全为异常低价时不硬选，本轮不推进（交回原流程如实告知）
            hotel = pick_best_hotel(list(seg.hotel_list))
            if hotel is None:
                break
            texts.append(
                f"{head}：{hotel.hotelName}，{hotel.pricePerNight:g}元/晚，总价 {hotel.totalPrice:g}元"
            )
            if float(hotel.pricePerNight) > 0:     # ★ 同一次循环产出数字账本
                prices[seg.city] = float(hotel.pricePerNight)
        elif seg.brand_list:
            texts.append(
                f"{head}：{seg.brand_list[0].brandName}（实时报价不可用，仅选定品牌，"
                f"具体门店到店择优选同档位）"
            )
    if len(texts) != len(payload.segments):
        app_logger.warning(
            f"⚠️ 住宿自动模式代选不完整（{len(texts)}/{len(payload.segments)} 站有候选），本轮不推进"
        )
        return []
    agent, config, _ = await get_agent_and_state(conversation_id)
    extra, step = _next_step(state)
    upd: dict = {
        "selected_hotels": texts, "stay_cursor": len(texts), "current_step": step,
        "stay_prices": prices,      # ★ 数字账本：第 11 章预算工具直接读，不解析展示文本
        # 档位必须**无条件写入**（表单未预选时兜底经济型）。
        # 行程步 requires 硬校验 selected_accommodation_types，字段缺失会直接抛错卡死流程
        "selected_accommodation_types": _accom_types(state) or ["economy_hotel"],
        **extra,
    }
    await agent.aupdate_state(config, upd)
    app_logger.info(f"✅ 住宿自动模式已代选 {len(texts)} 站，推进到 {step}")
    return texts


async def apply_hotel_selection(
    conversation_id: str, payload: HotelMultiSegmentSubmitPayload
) -> HotelMultiSegmentSubmitResult:
    """前端一次性提交 → 校验 + 写住宿账本 + 推进到餐饮/行程步（**全程不做日期校验**）"""
    result = validate_hotel_multi_submission(payload, cache_key=conversation_id)
    if not result.ok:
        return result
    agent, config, state = await get_agent_and_state(conversation_id)
    # 步骤守卫（代码层，不看模型措辞）：只有仍停在住宿步才允许落库推进——
    # 重复提交（前端连点/已确认过）按幂等成功处理，避免把已推进的状态机拉回住宿步
    step = str(state.get("current_step") or "")
    if step != "accommodation_planning":
        if step in _AFTER_STAY:
            return HotelMultiSegmentSubmitResult(
                ok=True, code="OK", msg="该批住宿已确认，无需重复提交",
                accepted=list(payload.segments),
                texts=list(state.get("selected_hotels") or []),
                next_step=step,      # 幂等：反馈文案用当前实际所在步骤
            )
        return HotelMultiSegmentSubmitResult(
            ok=False, code="STEP_MISMATCH",
            msg=f"当前流程步骤为 {step or '未知'}，不在住宿确认阶段，无法提交住宿",
        )
    extra, nxt = _next_step(state)
    upd: dict = {
        "selected_hotels": result.texts, "stay_cursor": len(result.texts),
        "stay_prices": dict(result.prices),   # ★ 校验器已经算好，这里只负责落库
        "current_step": nxt,
        # 档位无条件写入（表单未预选 → 兜底经济型），
        # 否则行程步 requires 校验 selected_accommodation_types 缺失直接抛错
        "selected_accommodation_types": _accom_types(state) or ["economy_hotel"],
        **extra,
    }
    await agent.aupdate_state(config, upd)
    app_logger.info(f"🏨 多段住宿已提交并落库：{len(result.texts)} 站，推进到 {nxt}")
    return result.model_copy(update={
        "msg": f"已确认 {len(result.texts)} 站住宿，继续为您安排餐饮与行程",
        "next_step": nxt,   # 提交路由据此挑续跑反馈文案（行程步 → "正在生成行程报告"）
    })


__all__ = [
    "hotel_card_mode",
    "emit_hotel_skill_ui",
    "auto_select_hotels",
    "apply_hotel_selection",
    "build_lead_text",
]