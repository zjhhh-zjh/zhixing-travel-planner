"""
多段住宿选择 Agent Skill —— Agent 工具实现（skill_name = hotel_multi_segment）

设计要点（与 train_multi_segment 完全同构）：
  1. 【真实数据】不内置 mock，复用项目已有酒店 MCP 工具（searchHotels / find-hotels 系）；
  2. 【并行】段与段用 asyncio.gather 并发查询（段数动态 2/3/多段，总耗时≈最慢一段）；
  3. 【分段解析】每段独立解析原始返回文本、**独立判定 mode**——同一份 payload 里
     h1 可以是门店模式、h2 可以是品牌模式，互不影响；
  4. 【身份唯一】候选 id 段内唯一：`{segment_id}-{序号}`（h1-1 / b2-3），多段不共号，防止串段；
  5. 【不阻断】某段工具失败/解析不出门店 → **该段**降级品牌模式，其它段照常，绝不让对话卡死；
  6. 【提交侧】只做基础合法性校验（段存在、mode 匹配、id 在候选内、每段都选了），
     **不做任何日期/时间校验**。

解析核心（三级兜底：JSON → 结构化取值 → 文本行解析）在同包 **hotel_parser.py**，
全项目只维护一份解析口径；本模块只负责「按段并行调度 + 段内 id 前缀化 + 多段校验」。
"""
import asyncio
from typing import Any, Optional

from langchain.tools import tool
from langchain_core.tools import BaseTool

# 解析核心（MCP 工具定位 / 入参形态 / 三级解析 / 品牌兜底）在同包的 hotel_parser.py，
# 全项目只维护一份口径；本模块只负责「按段并行调度 + 段内 id 前缀化 + 多段校验」
from app.capabilities.base import PayloadCache
from app.capabilities.hotel_multi_segment.hotel_parser import (
    brand_fallback,
    build_args,
    hotels_from_raw,
    resolve_hotel_tool,
    stay_nights,
)
from app.capabilities.hotel_multi_segment.hotel_skill_schema import (
    HOTEL_MULTI_SEGMENT_SKILL,
    HotelMultiSegmentItem,
    HotelMultiSegmentPayload,
    HotelMultiSegmentSubmitPayload,
    HotelMultiSegmentSubmitResult,
    HotelSegmentInput,
    HotelSubmitSelection,
)
from app.utils.logger import app_logger

# 最近一次下发给前端的 payload：提交侧校验的权威真源（前端展示的就是这一份）
# 按 cache_key 分桶（调用方传 conversation_id），避免多会话并发互相覆盖
_PAYLOAD_CACHE: PayloadCache[HotelMultiSegmentPayload] = PayloadCache("hotel")


def get_cached_payload(cache_key: Optional[str] = None) -> Optional[HotelMultiSegmentPayload]:
    """取某会话最近下发的候选 payload（提交校验用）；不存在返回 None"""
    return _PAYLOAD_CACHE.get(cache_key)


# 异常低价阈值：接口偶发返回"14元/晚"这类失真价格，低于此视为不可用
_MIN_HOTEL_PRICE = 25.0


def hotel_rank(price: float, score: float) -> tuple:
    """门店择优排序键（越小越优，供 min 代选）：异常低价排最后；评分≥4.5 按价格升序；
    评分<4.5 按评分降序、同分取更便宜（用负分折算成升序键）。

    状态机自动代选（hotel_flow.auto_select_hotels）与快速通道（express._pick_hotel）共用本函数，
    保证同一批候选在两通道选出同一家门店——此前状态机取首条、快速通道择优选，会漂移。
    """
    if float(price) < _MIN_HOTEL_PRICE:
        return (2, float("inf"), 0.0)
    if float(score) >= 4.5:
        return (0, float(price), 0.0)
    return (1, -float(score), float(price))


def pick_best_hotel(options: list[Any]) -> Optional[Any]:
    """门店候选择优；全部为异常低价时返回 None（不硬选，交回调用方如实告知）

    「异常低价」阈值与 hotel_rank 同源（_MIN_HOTEL_PRICE），避免两处各写一个数。
    """
    if not options:
        return None
    best = min(options, key=lambda o: hotel_rank(o.pricePerNight, o.starRating))
    return None if float(best.pricePerNight) < _MIN_HOTEL_PRICE else best


def _reid(segment_id: str, items: list[Any], field: str) -> list[Any]:
    """给候选加**段内唯一**前缀 id：h1-1 / h1-2（门店 hotel_id）、b2-1（品牌 brand_id）

    单段解析产出的 id 是 h1/h2 与 b1/b2，多段拼在一起会串号（第 2 段的 h1 与第 1 段同名），
    故按 `{segment_id}-{序号}` 重写 id，作为提交与校验的唯一身份。
    """
    return [item.model_copy(update={field: f"{segment_id}-{index}"})
            for index, item in enumerate(items, 1)]

async def _query_one_segment(
    tool: Optional[BaseTool], segment_id: str, segment: HotelSegmentInput, limit: int
) -> HotelMultiSegmentItem:
    """查询单段住宿（由 asyncio.gather 并行调度）

    任何异常/解析不出门店都**只影响本段**：该段降级为品牌模式，其它段照常返回。
    """
    nights = stay_nights(segment.checkInDate, segment.checkOutDate)
    options: list[Any] = []
    if tool is not None:
        args = build_args(
            tool, segment.city, segment.checkInDate, nights,
            segment.guestCount, segment.priceLevel, limit,
        )
        try:
            raw = await tool.ainvoke(args)
            options = hotels_from_raw(raw, nights, limit)
        except Exception as exc:                  # 单段服务抖动/超时/配额：本段降级，不影响其它段
            app_logger.warning(f"⚠️ hotel_multi_segment {segment_id}（{segment.city}）查询失败，本段降级品牌模式：{exc}")
    base = {
        "segment_id": segment_id, "city": segment.city, "checkInDate": segment.checkInDate,
        "checkOutDate": segment.checkOutDate, "guestCount": segment.guestCount,
        "priceLevel": segment.priceLevel,
    }
    if options:                                   # 场景 A：真实门店可用 → mode="hotel"
        return HotelMultiSegmentItem(
            **base, mode="hotel", hotel_list=_reid(segment_id, options, "hotel_id"), brand_list=[],
        )
    # 场景 B：实时报价不可用 → mode="brand"（按档位给常见连锁品牌，不编造门店与价格）
    return HotelMultiSegmentItem(
        # ★ 查不到门店就降级品牌模式：只给品牌，不编门店和价格
        **base, mode="brand", hotel_list=[],
        brand_list=_reid(segment_id, brand_fallback(segment.priceLevel), "brand_id"),
    )


async def build_hotel_multi_segment_payload(
    segments: list[Any],
    max_options_per_segment: int = 10,
    cache_key: Optional[str] = None,
) -> HotelMultiSegmentPayload:
    """核心函数（非工具）：并行查询全部住宿段 → 组装 payload，并缓存供提交侧校验

    段数由入参长度决定（2/3/多段均支持，不写死）；每段模式独立判定，可混合（h1 门店 + h2 品牌）。
    cache_key：隔离键（调用方传 conversation_id），保证提交侧校验到的是**本会话**的候选表。
    """
    try:
        parsed = [seg if isinstance(seg, HotelSegmentInput) else HotelSegmentInput.model_validate(seg)
                  for seg in segments]
    except Exception as exc:
        app_logger.warning(f"⚠️ hotel_multi_segment 入参不合法，已降级为空 payload：{exc}")
        return HotelMultiSegmentPayload(segments=[])
    if not parsed:
        return HotelMultiSegmentPayload(segments=[])
    tool = await resolve_hotel_tool()             # 工具缺席时每段都会自动走品牌兜底
    items = await asyncio.gather(*[                # ← 并行并发，非串行逐段查
        _query_one_segment(tool, f"h{index}", segment, max_options_per_segment)
        for index, segment in enumerate(parsed, 1)
    ])
    payload = HotelMultiSegmentPayload(segments=list(items))
    _PAYLOAD_CACHE.store(payload, cache_key)   # 供提交工具做「是否存在」校验
    app_logger.info(
        f"🏨 hotel_multi_segment 出参：{len(payload.segments)} 段 / "
        f"模式 {'、'.join(f'{s.segment_id}={s.mode}' for s in payload.segments)}"
    )
    return payload


# ==================== 提交侧：只做基础合法性校验（不做任何日期/时间校验） ====================
def _selection_text(segment: HotelMultiSegmentItem, item: HotelSubmitSelection) -> str:
    """已选定段的展示文本（供路由侧拼合成发言续跑后续步骤）"""
    head = f"{segment.city}（{segment.checkInDate} 至 {segment.checkOutDate}）"
    if item.mode == "hotel":
        picked = next((h for h in segment.hotel_list if h.hotel_id == item.hotel_id), None)
        if picked:
            return f"{head}：{picked.hotelName}，{picked.pricePerNight:g}元/晚，总价 {picked.totalPrice:g}元"
    picked = next((b for b in segment.brand_list if b.brand_id == item.brand_id), None)
    if picked:
        return f"{head}：{picked.brandName}（实时报价不可用，仅选定品牌，具体门店到店择优选同档位）"
    return head


def _selection_price(segment: HotelMultiSegmentItem, item: HotelSubmitSelection) -> float:
    """已选定段的每晚房价（元）；品牌兜底段没有实时报价，返回 0 表示"不计入"

    和 _selection_text 读的是同一条候选、同一个字段，区别只在一个给人看、一个给代码算。
    与其让下游从"358元/晚"这串文案里抠出 358，不如在这里直接把数字交出去
    """
    if item.mode != "hotel":
        return 0.0
    picked = next((h for h in segment.hotel_list if h.hotel_id == item.hotel_id), None)
    return float(picked.pricePerNight) if picked else 0.0


def validate_hotel_multi_submission(
    payload: HotelMultiSegmentSubmitPayload, cache_key: Optional[str] = None
) -> HotelMultiSegmentSubmitResult:
    """校验：段是否存在、mode 是否与下发一致、id 是否在该段候选内、是否每段都选了"""
    cached = get_cached_payload(cache_key)
    if cached is None:                            # 没查过候选就提交：拦下，避免写进不存在的候选
        return HotelMultiSegmentSubmitResult(
            ok=False, code="NO_PAYLOAD",
            msg="后端尚无住宿候选记录，请先获取住宿卡片后再提交",
        )
    items = payload.segments
    if not items:
        return HotelMultiSegmentSubmitResult(
            ok=False, code="EMPTY_SELECTION", msg="提交内容为空，每段住宿都需要选择",
        )
    by_id = {seg.segment_id: seg for seg in cached.segments}
    unknown = [item.segment_id for item in items if item.segment_id not in by_id]
    if unknown:
        return HotelMultiSegmentSubmitResult(
            ok=False, code="SEGMENT_NOT_FOUND",
            msg=f"不存在的住宿段：{'、'.join(unknown)}；合法段为 {'、'.join(by_id)}",
        )
    for item in items:                            # 逐段校验 mode 与身份 id 是否成对且合法
        segment = by_id[item.segment_id]
        # ★ 提交的模式必须和下发的一致
        if item.mode != segment.mode:
            return HotelMultiSegmentSubmitResult(
                ok=False, code="MODE_MISMATCH",
                msg=f"{item.segment_id} 段下发模式为 {segment.mode}，提交的却是 {item.mode}，请重新选择",
            )
        if item.mode == "hotel":
            if not item.hotel_id.strip():
                return HotelMultiSegmentSubmitResult(
                    ok=False,
                    code="MISSING_HOTEL_ID" if item.brand_id.strip() else "EMPTY_SELECTION",
                    msg=f"{item.segment_id} 段尚未选择酒店门店，请在卡片中选中一家后提交",
                )
            ids = {h.hotel_id for h in segment.hotel_list}
            if item.hotel_id not in ids:
                return HotelMultiSegmentSubmitResult(
                    ok=False, code="HOTEL_NOT_FOUND",
                    msg=f"{item.segment_id} 段的酒店 {item.hotel_id} 不在候选列表中"
                        f"（合法候选：{'、'.join(sorted(ids)) or '无'}）",
                )
        else:
            if not item.brand_id.strip():
                return HotelMultiSegmentSubmitResult(
                    ok=False,
                    code="MISSING_BRAND_ID" if item.hotel_id.strip() else "EMPTY_SELECTION",
                    msg=f"{item.segment_id} 段尚未选择酒店品牌，请在卡片中选中一个品牌后提交",
                )
            ids = {b.brand_id for b in segment.brand_list}
            if item.brand_id not in ids:
                return HotelMultiSegmentSubmitResult(
                    ok=False, code="BRAND_NOT_FOUND",
                    msg=f"{item.segment_id} 段的品牌 {item.brand_id} 不在候选列表中"
                        f"（合法候选：{'、'.join(sorted(ids)) or '无'}）",
                )
    chosen = {item.segment_id for item in items}
    missing = [seg.segment_id for seg in cached.segments if seg.segment_id not in chosen]
    if missing:
        return HotelMultiSegmentSubmitResult(
            ok=False, code="INCOMPLETE", msg=f"以下住宿段尚未选择：{'、'.join(missing)}",
        )
    by_item = {item.segment_id: item for item in items}
    # ★ 一次遍历同时产出两份账本：展示文本 + 每晚房价
    picked = {seg.city: _selection_price(seg, by_item[seg.segment_id]) for seg in cached.segments}
    return HotelMultiSegmentSubmitResult(
        ok=True, code="OK",
        msg=f"已收到 {len(items)} 段住宿选择，基础合法性校验通过（未做任何日期校验）",
        accepted=items,
        texts=[_selection_text(seg, by_item[seg.segment_id]) for seg in cached.segments],
        prices={city: price for city, price in picked.items() if price > 0},
    )


# ==================== Agent 可调用的两个 Tool ====================

@tool
async def query_hotel_multi_segment_tool(
    segments: list[dict],
    max_options_per_segment: int = 5,
) -> str:
    """并行查询多段住宿候选，返回可直接下发前端的 hotel_multi_segment skill payload。

    适用于「多目的地闭环行程」需要一次性给出每站住宿候选的场景（如 石家庄→上海→石家庄）。
    段与段之间**并发查询**，总耗时≈最慢一段；段数由 segments 长度决定，不是串行逐段查。
    **每段独立判定模式**：能查到真实门店该段为 mode="hotel"，查不到则降级 mode="brand"，
    同一份 payload 里两种模式可以混合（例如 h1 门店 + h2 品牌）。
    本工具只查询与组装，不做推荐排序、不锁房、不下单。

    Args:
        segments: 住宿段列表，按行程顺序排列。每项含 city（城市）、checkInDate（入住
            YYYY-MM-DD）、checkOutDate（离店 YYYY-MM-DD）、guestCount（人数，可选）、
            priceLevel（档位，可选）。
        max_options_per_segment: 每段向 MCP 请求的门店候选上限，默认 5。

    Returns:
        JSON 文本 {"skill_name":"hotel_multi_segment","segments":[...]}。
        **请原样下发给前端 UI Skill**：不要改写字段名、不要增删 segments、不要自己再校验日期；
        某段 mode="brand" 时如实告知用户"该城市实时酒店报价暂不可用，仅可选择品牌"。
    """
    payload = await build_hotel_multi_segment_payload(segments, max_options_per_segment)
    return payload.model_dump_json()


@tool
async def submit_hotel_multi_segment_tool(segments: list[dict]) -> str:
    """接收前端一次性回传的全部住宿段选择，只做基础合法性校验。

    Args:
        segments: 前端 onSubmit 抛回的数组，每项为
            {"segment_id": "h1", "mode": "hotel", "hotel_id": "h1-1", "brand_id": ""}
            或 {"segment_id": "h2", "mode": "brand", "hotel_id": "", "brand_id": "b2-2"}。
            mode 必须与该段下发 payload 的 mode 一致；id 必须是该段候选里的 id。

    Returns:
        JSON 文本 {"ok": bool, "code": str, "msg": str, "accepted": [...], "texts": [...]}。
        ok 为 false 时把 msg 原样告知用户，并请其在界面上改选后重新提交。
    """
    try:
        payload = HotelMultiSegmentSubmitPayload.model_validate({"segments": segments})
    except Exception as exc:                      # 结构不合法也走确定性回执，不抛错
        return HotelMultiSegmentSubmitResult(
            ok=False, code="INVALID_PAYLOAD", msg=f"提交数据结构不合法：{exc}"
        ).model_dump_json()
    result = validate_hotel_multi_submission(payload)
    app_logger.info(f"🏨 hotel_multi_segment 提交校验：{result.code} / {result.msg}")
    return result.model_dump_json()


__all__ = [
    "HOTEL_MULTI_SEGMENT_SKILL",
    "build_hotel_multi_segment_payload",
    "get_cached_payload",
    "hotel_rank",
    "pick_best_hotel",
    "validate_hotel_multi_submission",
    "query_hotel_multi_segment_tool",
    "submit_hotel_multi_segment_tool",
]