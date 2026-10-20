"""多段航班行程 Agent Skill —— Agent 工具实现（skill_name = flight_multi_segment）

设计要点（与多段高铁同构）：
  1. 【真实数据】不内置 mock，复用项目已有 VariFlight 航班 MCP 工具（searchFlightsByDepArr）；
  2. 【并行】段与段之间用 asyncio.gather 并发查询（段数动态，总耗时≈最慢一段）；
  3. 【只组装】拿原始返回 → parse_flight_rows 解析 → 组装成 FlightMultiSegmentPayload；
  4. 【零时间校验】全程不比对到达/起飞先后，不做逐段确认、不锁票；
  5. 【提交侧】只校验 segment_id / option_id 是否存在（option_id = 航班号-起飞时刻-到达机场）。
"""
import asyncio
from typing import Any, Optional

from langchain.tools import tool
from langchain_core.tools import BaseTool

from app.capabilities.base import PayloadCache
from app.capabilities.flight_multi_segment.flight_skill_schema import (
    FlightLeg,
    FlightMultiSegmentPayload,
    FlightOption,
    FlightSegment,
    FlightSubmitPayload,
    FlightSubmitResult,
)
from app.tools.mcp_tools import (
    _CITY_AIRPORT,
    _CITY_IATA,
    get_flight_tools,
    parse_flight_prices,
    parse_flight_rows,
)
from app.utils.logger import app_logger

# 查询工具缓存：多段并发时只解析一次工具列表，段间复用同一工具对象
_QUERY_TOOL: Optional[BaseTool] = None

# 最近一次下发给前端的 payload：提交侧校验的权威真源（前端展示的就是这一份）
# 按 cache_key 分桶（交通步传 conversation_id）——避免多会话并发互相覆盖候选表
_PAYLOAD_CACHE: PayloadCache[FlightMultiSegmentPayload] = PayloadCache("flight")

# 直查工具名（与 chat_stream_executor 早期的航班直查同口径）
_FLIGHT_QUERY_TOOL_NAME = "searchflightsbydeparr"

# ★ 票价工具：searchFlightsByDepArr 是**航班动态**接口（时刻/机型/准点率），
#   多数航线根本不返回票价——这就是卡片一直显示"价格待定"的根因。
#   VariFlight 另有独立的票价工具，按名字候选表定位（不写死单个名字，换版本改名也能接上）。
_PRICE_TOOL_NAMES = ("getflightpricebycities", "searchflightitineraries")
_PRICE_TOOL: Optional[BaseTool] = None
_PRICE_TOOL_RESOLVED = False      # ← 解析过就不再重复找（找不到时也只警告一次）


def get_cached_payload(cache_key: Optional[str] = None) -> Optional[FlightMultiSegmentPayload]:
    """取某会话最近下发的候选 payload（提交校验用）；不存在返回 None"""
    return _PAYLOAD_CACHE.get(cache_key)


async def _resolve_query_tool() -> Optional[BaseTool]:
    """定位航班查询工具 searchFlightsByDepArr；不可用返回 None（由调用方降级为空段）"""
    global _QUERY_TOOL
    if _QUERY_TOOL is not None:
        return _QUERY_TOOL
    for item in await get_flight_tools():
        if item.name.lower() == _FLIGHT_QUERY_TOOL_NAME:
            _QUERY_TOOL = item
            app_logger.info(f"✈️ flight_multi_segment 复用航班工具：{item.name}")
            return _QUERY_TOOL
    app_logger.warning("⚠️ flight_multi_segment 未找到航班查询工具（searchFlightsByDepArr）")
    return None


async def _resolve_price_tool() -> Optional[BaseTool]:
    """定位票价工具；找不到返回 None（本段票价留空，卡片照常展示时刻，只是没有价）"""
    global _PRICE_TOOL, _PRICE_TOOL_RESOLVED
    if _PRICE_TOOL_RESOLVED:
        return _PRICE_TOOL
    _PRICE_TOOL_RESOLVED = True
    tools = await get_flight_tools()
    for name in _PRICE_TOOL_NAMES:                 # 按候选表的先后顺序挑，先命中先用
        for item in tools:
            if item.name.lower() == name:
                _PRICE_TOOL = item
                app_logger.info(
                    f"💰 flight_multi_segment 票价工具：{item.name} "
                    f"（入参 schema：{_schema_props(item)}）"
                )
                return _PRICE_TOOL
    app_logger.warning(
        f"⚠️ flight_multi_segment 未找到票价工具（已试 {_PRICE_TOOL_NAMES}；"
        f"当前航班工具：{[t.name for t in tools]}），本次票价留空"
    )
    return None


def _schema_props(tool: BaseTool) -> list[str]:
    """取工具声明的入参名（MCP 工具的 args_schema 是 JSON Schema dict）"""
    schema = tool.args_schema if isinstance(tool.args_schema, dict) else {}
    return list((schema.get("properties") or {}).keys())


def _classify_arg(name: str) -> str:
    """把工具声明的参数名归到 date / dep / arr 三类；认不出返回空串

    票价接口的参数名各家不一（depCityCode / fromCity / origin / dep…），
    写死一套名字换个版本就全查空，故按关键词归类——与 mcp_tools 的车票入参守卫 _TRAIN_ARG_ALIAS
    同一思路：**参数合法性归代码，不靠提示词**。
    """
    low = name.lower()
    if "date" in low or low in ("day", "depdate", "flightdate"):
        return "date"
    # ★ 先判到达：arrCity / toCity / destination / arrival 里都含 "to"，
    #   若先判出发会把 "toCity" 错认成出发地（顺序颠倒会把整段航线查反）
    if any(k in low for k in ("arr", "dest", "to_", "tocity", "tocode", "end")) or low in ("to", "dst"):
        return "arr"
    if any(k in low for k in ("dep", "from", "orig", "start")) or low in ("dep", "src"):
        return "dep"
    return ""


def _price_args(tool: BaseTool, date: str, dep_city: str, arr_city: str) -> Optional[dict]:
    """按工具自己声明的参数名组装票价查询入参；必填项凑不齐返回 None（不盲调）"""
    schema = tool.args_schema if isinstance(tool.args_schema, dict) else {}
    props = list((schema.get("properties") or {}).keys())
    required = list(schema.get("required") or [])
    value = {"date": date, "dep": dep_city, "arr": arr_city}
    args = {}
    for prop in props:
        kind = _classify_arg(prop)
        if kind and value.get(kind):
            args[prop] = value[kind]
    missing = [r for r in required if not args.get(r)]
    if missing:
        app_logger.warning(
            f"⚠️ 票价工具 {tool.name} 必填参数无法自动匹配：缺 {missing}"
            f"（工具声明 {props}）；本段票价留空，请在 _classify_arg 里补一条映射"
        )
        return None
    return args


async def _query_leg_prices(
    tool: Optional[BaseTool], segment_id: str, leg: "FlightLeg", travel_date: str
) -> tuple[dict[str, float], float]:
    """查一段航线的票价 → (按航班号票价表, 航线最低价)；任何失败都降级为 ({}, 0.0)

    ★ 工具由调用方传进来（与 query_tool 同一口径）：多段并发时只解析一次，
      段间复用同一个工具对象；传 None 表示票价不可用，本段票价留空。
    """
    if tool is None:
        return {}, 0.0
    # ★ 票价接口要**城市**三字码，与时刻表要的**机场**码是两张表，别混用
    dep, arr = _CITY_IATA.get(leg.from_), _CITY_IATA.get(leg.to)
    if not dep or not arr:
        app_logger.warning(
            f"⚠️ flight_multi_segment {segment_id}（{leg.from_}→{leg.to}）无城市码，跳过票价查询"
        )
        return {}, 0.0
    args = _price_args(tool, travel_date, dep, arr)
    if args is None:
        return {}, 0.0
    try:
        raw = await tool.ainvoke(args)
    except Exception as exc:      # 票价失败绝不拖垮时刻表：卡片照出，只是没有价
        app_logger.warning(f"⚠️ flight_multi_segment {segment_id} 票价查询异常：{exc}")
        return {}, 0.0
    by_flight, lowest = parse_flight_prices(raw)
    app_logger.info(
        f"💰 flight_multi_segment {segment_id} {leg.from_}→{leg.to} 票价："
        f"{len(by_flight)} 班有价，航线最低 {lowest or '—'}"
    )
    return by_flight, lowest


def _duration(dep: str, arr: str) -> str:
    """起飞/到达时刻算历时 HH:mm；跨天或缺失（arr < dep）时留空，不编造"""
    try:
        dh, dm = (int(x) for x in dep.split(":"))
        ah, am = (int(x) for x in arr.split(":"))
    except Exception:
        return ""
    minutes = (ah * 60 + am) - (dh * 60 + dm)
    if minutes <= 0:            # 跨天或解析异常：留空（不做猜测）
        return ""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def option_id_of(row: dict) -> str:
    """段内唯一标识：航班号-起飞时刻-到达机场（航班号当身份不唯一，故用本标识）"""
    return f"{row.get('code')}-{row.get('dep')}-{row.get('to')}"


def _rows_to_options(
    rows: list[dict], limit: int, prices: Optional[dict[str, float]] = None
) -> list[FlightOption]:
    """航班原始行 → FlightOption：按起飞时间升序取前 limit 班（仅排序取数，不做时间校验）

    价格字段三级取值（依次回退，**取不到就是 0，绝不编造**）：
      ① 时刻表行自带的票价（parse_flight_rows 按候选键抠出来的 row["price"]）
      ② 票价接口按航班号返回的价（prices[航班号]）
      ③ 都没有 → 0，前端展示"价格待定"，代选时按"价格未知"处理
    """
    table = prices or {}
    options: list[FlightOption] = []
    for row in sorted(rows, key=lambda r: r.get("dep") or ""):
        code = row.get("code") or ""
        # ★ 时刻表自带价优先（同一接口同一班，最不会串）；其次才查票价接口的表
        price = float(row.get("price") or 0) or float(table.get(code.upper(), 0) or 0)
        options.append(FlightOption(
            flight_no=code,
            dep_time=row.get("dep") or "",
            arr_time=row.get("arr") or "",
            dep_airport=row.get("from") or "",
            arr_airport=row.get("to") or "",
            price=price,
            duration=_duration(row.get("dep") or "", row.get("arr") or ""),
            option_id=option_id_of(row),
        ))
        if len(options) >= limit:
            break
    return options


async def _query_one_leg(
    query_tool: BaseTool, price_tool: Optional[BaseTool],
    segment_id: str, leg: FlightLeg, limit: int
) -> FlightSegment:
    """查询单段航班（由 asyncio.gather 并行调度）；任何异常都降级为空 flight_list，绝不抛出"""
    travel_date = (leg.travel_date or "").strip()
    # ★ 城市先换成机场码；没有码的城市不查、不猜
    dep, arr = _CITY_AIRPORT.get(leg.from_), _CITY_AIRPORT.get(leg.to)
    if not dep or not arr:      # 城市未收录机场码：不放行直查，降级为空段
        app_logger.warning(
            f"⚠️ flight_multi_segment {segment_id}（{leg.from_}→{leg.to}）无机场码，跳过查询"
        )
        return FlightSegment(
            segment_id=segment_id, from_=leg.from_, to=leg.to,
            date=travel_date, flight_list=[],
        )
    # ★ 时刻表与票价**并发**查（票价是另一个接口，串行会把单段耗时翻倍）；
    #   return_exceptions=True 保证票价炸了也不影响时刻表——卡片照出，只是没有价
    schedule_raw, price_pair = await asyncio.gather(
        query_tool.ainvoke({"date": travel_date, "dep": dep, "arr": arr}),
        _query_leg_prices(price_tool, segment_id, leg, travel_date),
        return_exceptions=True,
    )
    if isinstance(schedule_raw, Exception):  # 单段失败不影响其它段，前端对空段置灰
        app_logger.warning(
            f"⚠️ flight_multi_segment {segment_id}（{leg.from_}→{leg.to}）查询异常：{schedule_raw}"
        )
        return FlightSegment(
            segment_id=segment_id, from_=leg.from_, to=leg.to,
            date=travel_date, flight_list=[],
        )
    by_flight, lowest = ({}, 0.0) if isinstance(price_pair, Exception) else price_pair
    rows = parse_flight_rows(schedule_raw)
    options = _rows_to_options(rows, limit, by_flight)
    priced = sum(1 for o in options if o.price > 0)
    app_logger.info(
        f"✈️ flight_multi_segment {segment_id} {leg.from_}→{leg.to} {travel_date} "
        f"解析 {len(rows)} 班 → 取 {len(options)} 班（{priced} 班有票价）"
    )
    return FlightSegment(
        segment_id=segment_id, from_=leg.from_, to=leg.to,
        date=travel_date, flight_list=options,
        # ★ 段级参考价：票价接口只给"XX元起"时放这里，**不**挨个填到每一班上（那是编造）
        low_price=lowest,
    )


async def build_flight_multi_segment_payload(
    legs: list[Any],
    max_options_per_leg: int = 10,
    cache_key: Optional[str] = None,
) -> FlightMultiSegmentPayload:
    """核心函数（非工具）：并行查询全部段 → 组装 payload，并缓存供提交侧校验

    legs 长度即段数；日期为空时原样留空由前端展示，不推断时序、不校验时间先后。
    数据源不可用 / 解析为空时返回**空段**而不是抛异常，避免打断对话。
    cache_key：隔离键（调用方传 conversation_id），保证提交侧校验到的是**本会话**的候选表
    """
    try:
        parsed = [leg if isinstance(leg, FlightLeg) else FlightLeg.model_validate(leg) for leg in legs]
    except Exception as exc:
        app_logger.warning(f"⚠️ flight_multi_segment 入参不合法，已降级为空 payload：{exc}")
        return FlightMultiSegmentPayload(segments=[])
    if not parsed:
        return FlightMultiSegmentPayload(segments=[])
    query_tool = await _resolve_query_tool()
    if query_tool is None:                   # 数据源不可用：返回空段而不是抛错
        empty = FlightMultiSegmentPayload(segments=[
            FlightSegment(segment_id=f"s{i}", from_=leg.from_, to=leg.to,
                          date=(leg.travel_date or "").strip(), flight_list=[])
            for i, leg in enumerate(parsed, 1)
        ])
        _PAYLOAD_CACHE.store(empty, cache_key)
        return empty
    price_tool = await _resolve_price_tool()   # ★ 票价工具同样只解析一次，段间复用
    segments = await asyncio.gather(*[     # ← 并行并发，非串行逐段
        _query_one_leg(query_tool, price_tool, f"s{index}", leg, max_options_per_leg)
        for index, leg in enumerate(parsed, 1)
    ])
    payload = FlightMultiSegmentPayload(segments=list(segments))
    _PAYLOAD_CACHE.store(payload, cache_key)  # 供提交侧做「是否存在」校验
    return payload


# ==================== 提交侧：只做基础合法性校验（明确不做时间校验） ====================
def validate_flight_submission(
    payload: FlightSubmitPayload, cache_key: Optional[str] = None
) -> FlightSubmitResult:
    """校验 segment_id 是否存在、option_id 是否在该段候选列表内；不比对任何时间先后"""
    cached = get_cached_payload(cache_key)
    if cached is None:                        # 没查过航班就提交：拦下，避免写进不存在的航班
        return FlightSubmitResult(
            ok=False, code="NO_PAYLOAD",
            msg="后端尚无候选航班记录，请先在交通步重新查询航班后再提交",
        )
    items = payload.segments
    if not items:
        return FlightSubmitResult(
            ok=False, code="EMPTY_SELECTION", msg="提交内容为空，至少要选择一段航班",
        )
    no_id = [f"{item.segment_id}·{item.flight_no or '未填写航班号'}"
             for item in items if not (item.option_id or "").strip()]
    if no_id:
        return FlightSubmitResult(
            ok=False, code="MISSING_OPTION_ID",
            msg=f"以下选择缺少航班标识（option_id）：{'、'.join(no_id)}；"
                f"请在卡片中重新选择后提交",
        )
    by_id = {seg.segment_id: seg for seg in cached.segments}
    unknown = [item.segment_id for item in items if item.segment_id not in by_id]
    if unknown:
        return FlightSubmitResult(
            ok=False, code="SEGMENT_NOT_FOUND",
            msg=f"不存在的行程段：{'、'.join(unknown)}；合法段为 {'、'.join(by_id)}",
        )
    bad_flight = [
        f"{item.segment_id}·{item.option_id}" for item in items
        if item.option_id not in {f.option_id for f in by_id[item.segment_id].flight_list}
    ]
    if bad_flight:
        return FlightSubmitResult(
            ok=False, code="FLIGHT_NOT_FOUND",
            msg=f"以下航班不在该段候选列表中：{'、'.join(bad_flight)}",
        )
    chosen = {item.segment_id for item in items}
    # ★ 实时无候选（flight_list 为空/查询失败、卡片里置灰）的段不算"漏选"——否则该段无从选择、
    #   用户永远凑不齐全部段而卡在交通步。只有"有候选却没选"的段才判 INCOMPLETE。
    missing = [seg.segment_id for seg in cached.segments
               if seg.segment_id not in chosen and seg.flight_list]
    if missing:
        return FlightSubmitResult(
            ok=False, code="INCOMPLETE",
            msg=f"以下行程段尚未选择航班：{'、'.join(missing)}",
        )
    return FlightSubmitResult(
        ok=True, code="OK",
        msg=f"已收到 {len(items)} 段选中航班，基础合法性校验通过（未做任何时间校验）",
        accepted=items,
    )

# ==================== Agent 可调用的两个 Tool ====================

@tool
async def query_flight_multi_segment_tool(
    legs: list[dict],
    max_options_per_leg: int = 5,
) -> str:
    """并行查询多段航班，返回可直接下发前端的 flight_multi_segment skill payload。

    适用于「多目的地行程」需要一次性给出各段候选航班的场景。段与段之间**并发查询**，
    总耗时≈最慢一段，段数由 legs 长度决定。

    Args:
        legs: 行程段列表，按时间顺序排列。每项含 from（出发城市）、to（到达城市）、
            travel_date（该段乘机日期 YYYY-MM-DD）。
        max_options_per_leg: 每段返回的航班上限，默认 5。

    Returns:
        JSON 文本 {"skill_name":"flight_multi_segment","segments":[...]}。
        **请原样下发给前端 UI Skill**：不要改写字段名、不要增删 segments、不要自己再校验时间；
        某些段查不到航班时该段 flight_list 为空数组，如实告知用户即可。
    """
    payload = await build_flight_multi_segment_payload(legs, max_options_per_leg)
    app_logger.info(f"✈️ flight_multi_segment 出参：{len(payload.segments)} 段")
    return payload.model_dump_json(by_alias=True)


@tool
async def submit_flight_multi_segment_tool(segments: list[dict]) -> str:
    """接收前端一次性回传的全部段选中航班，只做基础合法性校验。

    **不做任何航班时间校验**（不比对到达/起飞先后）。

    Args:
        segments: 前端 onSubmit 抛回的数组，每项为
            {"segment_id": "s1", "option_id": "CA1501-08:00-SHA", "flight_no": "CA1501"}。
            **option_id 是身份**（航班号在一天内可能不唯一，不能当身份）。

    Returns:
        JSON 文本 {"ok": bool, "code": str, "msg": str, "accepted": [...]}。
        ok 为 false 时把 msg 原样告知用户，并请其在界面上改选后重新提交；
        ok 为 true 表示已确认全部段选择（未锁票、未做时间校验）。
    """
    try:
        payload = FlightSubmitPayload.model_validate({"segments": segments})
    except Exception as exc:                  # 结构缺失也走确定性回执，不抛错
        return FlightSubmitResult(
            ok=False, code="INVALID_PAYLOAD", msg=f"提交数据结构不合法：{exc}"
        ).model_dump_json()
    result = validate_flight_submission(payload)
    app_logger.info(f"✈️ flight_multi_segment 提交校验：{result.code} / {result.msg}")
    return result.model_dump_json()


__all__ = [
    "build_flight_multi_segment_payload",
    "get_cached_payload",
    "validate_flight_submission",
    "query_flight_multi_segment_tool",
    "submit_flight_multi_segment_tool",
]