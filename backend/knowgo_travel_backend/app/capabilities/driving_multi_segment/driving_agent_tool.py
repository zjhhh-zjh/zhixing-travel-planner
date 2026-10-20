"""多段自驾行程 Agent Skill —— Agent 工具实现（skill_name = driving_multi_segment）

设计要点（与多段航班同构）：
  1. 【真实数据】不内置 mock，复用项目已有高德驾车 MCP 工具（maps_direction_driving）；
  2. 【并行】段与段之间用 asyncio.gather 并发查询（段数动态，总耗时≈最慢一段）；
  3. 【只组装】拿原始返回 → parse_driving_rows 解析 → 组装成 DrivingMultiSegmentPayload；
  4. 【只读确认】每段只取**默认策略的最优一条路线**，不做策略可选、不做任何时间/路线校验；
  5. 【提交侧】只校验 segment_id 是否存在、段是否覆盖完整（无班次号/路线策略可校验）。
"""
import asyncio
from typing import Any, Optional

from langchain.tools import tool
from langchain_core.tools import BaseTool

from app.capabilities.base import PayloadCache
from app.capabilities.driving_multi_segment.driving_skill_schema import (
    DrivingLeg,
    DrivingMultiSegmentPayload,
    DrivingSegment,
    DrivingSubmitPayload,
    DrivingSubmitResult,
)
from app.tools.mcp_tools import _CITY_COORD, get_driving_tools, parse_driving_rows
from app.utils.logger import app_logger

# 查询工具缓存：多段并发时只解析一次工具列表，段间复用同一工具对象
_QUERY_TOOL: Optional[BaseTool] = None

# 最近一次下发给前端的 payload：提交侧校验的权威真源（前端展示的就是这一份）
# 按 cache_key 分桶（交通步传 conversation_id）——避免多会话并发互相覆盖候选表
_PAYLOAD_CACHE: PayloadCache[DrivingMultiSegmentPayload] = PayloadCache("driving")

# 驾车直查工具名（与 get_driving_tools 的筛选口径一致）
_DRIVING_QUERY_TOOL_NAME = "maps_direction_driving"


def get_cached_payload(cache_key: Optional[str] = None) -> Optional[DrivingMultiSegmentPayload]:
    """取某会话最近下发的候选 payload（提交校验用）；不存在返回 None"""
    return _PAYLOAD_CACHE.get(cache_key)


async def _resolve_query_tool() -> Optional[BaseTool]:
    """定位驾车路线工具 maps_direction_driving；不可用返回 None（由调用方降级为空段）"""
    global _QUERY_TOOL
    if _QUERY_TOOL is not None:
        return _QUERY_TOOL
    for item in await get_driving_tools():
        if _DRIVING_QUERY_TOOL_NAME in item.name.lower():
            _QUERY_TOOL = item
            app_logger.info(f"🚗 driving_multi_segment 复用驾车工具：{item.name}")
            return _QUERY_TOOL
    app_logger.warning("⚠️ driving_multi_segment 未找到驾车路线工具（maps_direction_driving）")
    return None


def _empty_segment(segment_id: str, leg: DrivingLeg) -> DrivingSegment:
    """查询失败/城市解析不出时的空段（ok=False，前端置灰；绝不编造数字）"""
    return DrivingSegment(
        segment_id=segment_id, from_=leg.from_, to=leg.to,
        date=(leg.travel_date or "").strip(), ok=False,
    )

async def _query_one_leg(
    query_tool: BaseTool, segment_id: str, leg: DrivingLeg
) -> DrivingSegment:
    """查询单段自驾（由 asyncio.gather 并行调度）；任何异常都降级为空段，绝不抛出"""
    travel_date = (leg.travel_date or "").strip()
    frm, to = (leg.from_ or "").strip(), (leg.to or "").strip()
    if not frm or not to:                       # 城市名缺失：不放行直查，降级为空段
        app_logger.warning(f"⚠️ driving_multi_segment {segment_id} 城市名缺失，跳过查询")
        return _empty_segment(segment_id, leg)
    # 已收录城市用中心坐标；未收录则退回城市名尝试（高德不接受时即为查询失败，仍降级空段）
    # ★ 城市换坐标；表里没有的城市原样传名字
    origin, dest = _CITY_COORD.get(frm, frm), _CITY_COORD.get(to, to)
    try:
        raw = await query_tool.ainvoke({"origin": origin, "destination": dest})
    except Exception as exc:                    # 单段失败不影响其它段，前端对空段置灰
        app_logger.warning(
            f"⚠️ driving_multi_segment {segment_id}（{frm}→{to}）查询异常：{exc}"
        )
        return _empty_segment(segment_id, leg)
    row = parse_driving_rows(raw)
    if not row:                                 # 解析不出任何字段：空段（ok=False）
        app_logger.warning(
            f"⚠️ driving_multi_segment {segment_id}（{frm}→{to}）返回无法解析，降级为空段"
        )
        return _empty_segment(segment_id, leg)
    segment = DrivingSegment(
        segment_id=segment_id, from_=frm, to=to, date=travel_date, ok=True,
        distance_km=float(row.get("distance_km") or 0),
        duration_text=str(row.get("duration_text") or ""),
        tolls=float(row.get("tolls") or 0),
        traffic_lights=int(row.get("traffic_lights") or 0),
        summary=str(row.get("summary") or ""),
    )
    app_logger.info(
        f"🚗 driving_multi_segment {segment_id} {frm}→{to} "
        f"解析：{segment.distance_km}km / {segment.duration_text or '—'} / "
        f"过路费{segment.tolls:g}元 / 红绿灯{segment.traffic_lights}"
    )
    return segment


async def build_driving_multi_segment_payload(
    legs: list[Any],
    cache_key: Optional[str] = None,
) -> DrivingMultiSegmentPayload:
    """核心函数（非工具）：并行查询全部段 → 组装 payload，并缓存供提交侧校验

    legs 长度即段数；每段只取默认策略的一条路线（只读确认，无策略可选）。
    数据源不可用 / 解析为空时返回**空段**而不是抛异常，避免打断对话。
    cache_key：隔离键（调用方传 conversation_id），保证提交侧校验到的是**本会话**的候选表
    """
    try:
        parsed = [leg if isinstance(leg, DrivingLeg) else DrivingLeg.model_validate(leg) for leg in legs]
    except Exception as exc:
        app_logger.warning(f"⚠️ driving_multi_segment 入参不合法，已降级为空 payload：{exc}")
        return DrivingMultiSegmentPayload(segments=[])
    if not parsed:
        return DrivingMultiSegmentPayload(segments=[])
    query_tool = await _resolve_query_tool()
    if query_tool is None:                      # 数据源不可用：返回空段而不是抛错
        empty = DrivingMultiSegmentPayload(segments=[
            _empty_segment(f"s{i}", leg) for i, leg in enumerate(parsed, 1)
        ])
        _PAYLOAD_CACHE.store(empty, cache_key)
        return empty
    segments = await asyncio.gather(*[        # ← 并行并发，非串行逐段
        _query_one_leg(query_tool, f"s{index}", leg)
        for index, leg in enumerate(parsed, 1)
    ])
    payload = DrivingMultiSegmentPayload(segments=list(segments))
    _PAYLOAD_CACHE.store(payload, cache_key)  # 供提交侧做「是否存在」校验
    return payload


# ==================== 提交侧：只做基础合法性校验（无策略/时间可校验） ====================

def validate_driving_submission(
    payload: DrivingSubmitPayload, cache_key: Optional[str] = None
) -> DrivingSubmitResult:
    """只读确认：校验 segment_id 是否存在、段是否覆盖完整；不做任何时间/路线校验"""
    cached = get_cached_payload(cache_key)
    if cached is None:                          # 没查过路线就提交：拦下，避免写进不存在的段
        return DrivingSubmitResult(
            ok=False, code="NO_PAYLOAD",
            msg="后端尚无自驾候选路线记录，请先在交通步重新查询路线后再提交",
        )
    items = payload.segments
    if not items:
        return DrivingSubmitResult(
            ok=False, code="EMPTY_SELECTION", msg="提交内容为空，至少要确认一段自驾路线",
        )
    by_id = {seg.segment_id: seg for seg in cached.segments}
    unknown = [item.segment_id for item in items if item.segment_id not in by_id]
    if unknown:
        return DrivingSubmitResult(
            ok=False, code="SEGMENT_NOT_FOUND",
            msg=f"不存在的行程段：{'、'.join(unknown)}；合法段为 {'、'.join(by_id)}",
        )
    chosen = {item.segment_id for item in items}
    # ★ 实时无候选（ok 为空/查询失败、卡片里置灰）的段不算"漏选"——否则该段无从选择、
    #   用户永远凑不齐全部段而卡在交通步。只有"有候选却没选"的段才判 INCOMPLETE。
    missing = [seg.segment_id for seg in cached.segments
               if seg.segment_id not in chosen and seg.ok]
    if missing:
        return DrivingSubmitResult(
            ok=False, code="INCOMPLETE",
            msg=f"以下行程段尚未确认路线：{'、'.join(missing)}",
        )
    return DrivingSubmitResult(
        ok=True, code="OK",
        msg=f"已收到 {len(items)} 段自驾路线确认，基础合法性校验通过（未做任何时间/路线校验）",
        accepted=items,
    )

# ==================== Agent 可调用的两个 Tool ====================

@tool
async def query_driving_multi_segment_tool(legs: list[dict]) -> str:
    """并行查询多段自驾路线，返回可直接下发前端的 driving_multi_segment skill payload。

    适用于「多目的地行程」需要一次性给出各段驾车路线的场景。段与段之间**并发查询**，
    总耗时≈最慢一段，段数由 legs 长度决定。每段只返回一条**默认策略的最优路线**。

    Args:
        legs: 行程段列表，按时间顺序排列。每项含 from（出发城市）、to（到达城市）、
            travel_date（该段出行日期 YYYY-MM-DD，自驾可不填）。

    Returns:
        JSON 文本 {"skill_name":"driving_multi_segment","segments":[...]}。
        **请原样下发给前端 UI Skill**：不要改写字段名、不要增删 segments、不要自己再校验；
        某些段查不到路线时该段 ok=false，如实告知用户即可。
    """
    payload = await build_driving_multi_segment_payload(legs)
    app_logger.info(f"🚗 driving_multi_segment 出参：{len(payload.segments)} 段")
    return payload.model_dump_json(by_alias=True)


@tool
async def submit_driving_multi_segment_tool(segments: list[dict]) -> str:
    """接收前端一次性回传的全部段确认（只读确认，无路线策略可选），只做基础合法性校验。

    **不做任何时间/路线校验**。

    Args:
        segments: 前端 onSubmit 抛回的数组，每项为 {"segment_id": "s1"}。

    Returns:
        JSON 文本 {"ok": bool, "code": str, "msg": str, "accepted": [...]}。
        ok 为 false 时把 msg 原样告知用户，并请其在界面上重新确认后提交；
        ok 为 true 表示已确认全部段路线（未做任何校验）。
    """
    try:
        payload = DrivingSubmitPayload.model_validate({"segments": segments})
    except Exception as exc:                  # 结构缺失也走确定性回执，不抛错
        return DrivingSubmitResult(
            ok=False, code="INVALID_PAYLOAD", msg=f"提交数据结构不合法：{exc}"
        ).model_dump_json()
    result = validate_driving_submission(payload)
    app_logger.info(f"🚗 driving_multi_segment 提交校验：{result.code} / {result.msg}")
    return result.model_dump_json()


__all__ = [
    "build_driving_multi_segment_payload",
    "get_cached_payload",
    "validate_driving_submission",
    "query_driving_multi_segment_tool",
    "submit_driving_multi_segment_tool",
]