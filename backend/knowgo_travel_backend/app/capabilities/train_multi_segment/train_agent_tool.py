"""
多段高铁行程 Agent Skill —— Agent 工具实现（skill_name = train_multi_segment）

设计要点：
  1. 【真实数据】不内置 mock，直接复用项目已有 12306 MCP 工具（get-tickets 系）查真实班次；
  2. 【并行】段与段之间用 asyncio.gather 并发查询（段数动态 2/3/4…，总耗时≈最慢一段）；
  3. 【只组装】拿原始返回 → parse_ticket_rows 解析 → 组装成 TrainMultiSegmentPayload；
  4. 【零时间校验】全程不比对到站/发车先后，不做逐段确认、不锁票；
  5. 【提交侧】只校验 segment_id / option_id 是否存在（option_id = 车次-发车时刻-到达站，
     因为车次号在同段可重复），同样不校验时间。
"""
import asyncio
from typing import Any, Optional

from langchain.tools import tool
from langchain_core.tools import BaseTool

from app.capabilities.base import PayloadCache
from app.capabilities.train_multi_segment.train_skill_schema import (
    TRAIN_MULTI_SEGMENT_SKILL,
    TrainLeg,
    TrainMultiSegmentPayload,
    TrainOption,
    TrainSegment,
    TrainSubmitPayload,
    TrainSubmitResult,
)
from app.tools.mcp_tools import get_all_mcp_tools, parse_compact_rows, parse_ticket_rows
from app.utils.logger import app_logger

# 席别优先序：与本项目 mcp_tools._SEAT_ORDER 同口径，保证"取票价"取到同一档
_SEAT_PRIORITY = ("二等座", "硬座", "一等座", "硬卧", "软卧", "无座")

# 查询工具缓存：多段并发时只解析一次全量工具列表，段间复用同一工具对象
_QUERY_TOOL: Optional[BaseTool] = None

# 最近一次下发给前端的 payload：提交侧校验的权威真源（前端展示的就是这一份）
# 按 cache_key 分桶（交通步传 conversation_id，模型自调工具时用默认桶 "_"）——
# 原先是单个进程级全局变量，多用户/多会话并发时会互相覆盖，提交侧校验到别人的候选表
_PAYLOAD_CACHE: PayloadCache[TrainMultiSegmentPayload] = PayloadCache("train")


def get_cached_payload(cache_key: Optional[str] = None) -> Optional[TrainMultiSegmentPayload]:
    """取某会话最近下发的候选 payload（提交校验用）；不存在返回 None"""
    return _PAYLOAD_CACHE.get(cache_key)


def train_score(minutes: int, price: float) -> float:
    """班次综合评分：历时(分钟) + 票价/10（1 分钟 ≈ 0.1 元），越低越优

    状态机自动代选（train_flow._pick_best）与快速通道（express._pick_train）共用本函数，
    保证同一份候选在两通道选出同一趟车——此前两处各写一套规则会漂移。
    """
    return minutes + float(price) / 10


async def _resolve_query_tool() -> Optional[BaseTool]:
    """定位 12306 车票查询工具：按「必填 date/fromStation/toStation」判定，不写死工具名

    注意取 get_all_mcp_tools() 的**原始工具**而非 get_train_tools() 的守卫版：
    守卫版把结果压成"一行一趟"的紧凑文本，本 Skill 需要结构化字段，故直接解析 MCP 原文。
    """
    global _QUERY_TOOL
    if _QUERY_TOOL is not None:
        return _QUERY_TOOL
    for item in await get_all_mcp_tools():
        schema = item.args_schema if isinstance(item.args_schema, dict) else {}
        # ★ 按"必填 date / fromStation / toStation"认 12306 工具，不写死工具名
        if {"date", "fromStation", "toStation"} <= set(schema.get("required") or []):
            _QUERY_TOOL = item
            app_logger.info(f"🚄 train_multi_segment 复用 12306 工具：{item.name}")
            return _QUERY_TOOL
    app_logger.warning("⚠️ train_multi_segment 未找到 12306 车票查询工具（date/fromStation/toStation）")
    return None


def _pick_seat(seats: dict, prefer: str) -> Optional[tuple[str, tuple[str, float]]]:
    """挑席别：优先用户期望席别 → 次按 _SEAT_PRIORITY → 最后退化为第一个返回席别"""
    if prefer and prefer in seats:
        return prefer, seats[prefer]
    for name in _SEAT_PRIORITY:
        if name in seats:
            return name, seats[name]
    if seats:
        name = next(iter(seats))
        return name, seats[name]
    return None


def _ticket_status(raw_status: str) -> str:
    """余票状态归一：有→有票，无→无票，候补保留，纯数字→N张（前端按字符串原样展示）"""
    text = (raw_status or "").strip()
    if text in ("有", "有票"):
        return "有票"
    if text in ("无", "无票", "--", "*", ""):
        return "无票" if text else "未知"
    if "候补" in text:
        return "候补"
    # ★ "整串都是数字"用 str.isdecimal() 就够了，判定与 re.fullmatch(r"\d+") 完全一致
    if text.isdecimal():
        return f"{text}张"
    return text

def _split_seat_tail(text: str) -> tuple[str, str, float] | None:
    """紧凑行的席位尾巴「二等座 有 553元」→ (席别, 余票, 票价)；形态不符返回 None

    这串文本是**我们自己的代码**拼的（环节 4 的 compact_ticket_text）：三段、空格分隔、
    票价以"元"收尾，形态完全确定，所以 split 就够了，不需要正则。
    早期实现用的是一条"三组非空白字符 + 以元结尾的数字"的正则，除了难读还有个隐患：
    像"5.5.3元"这种脏数据它会判定命中，随后 float() 抛 ValueError 把整段查询带崩；
    这里 float 失败一律返回 None，坏数据只影响它自己那一趟
    """
    parts = (text or "").strip().split()
    # ★ str.split() 按空白切、自动丢空串，等价于正则里那几个 \S+，但一眼能看懂
    if len(parts) != 3 or not parts[2].endswith("元"):
        return None
    try:
        return parts[0], parts[1], float(parts[2][:-1])
    except ValueError:
        return None


def _compact_to_rows(raw: Any) -> list[dict]:
    """兜底解析：万一拿到的工具已被守卫包装（输出紧凑行），还原成与 parse_ticket_rows 同构的行"""
    rows: list[dict] = []
    for item in parse_compact_rows(raw):
        seat = _split_seat_tail(item.get("seat") or "")
        rows.append({
            "code": item["code"], "from": item["from"], "to": item["to"],
            "dep": item["dep"], "arr": item["arr"], "dur": item["dur"],
            # ★ 解析不出席位尾巴就给空字典，交给 _rows_to_options 跳过这一趟
            "seats": {seat[0]: (seat[1], seat[2])} if seat else {},
        })
    return rows


def option_id_of(row: dict) -> str:
    """段内唯一标识：车次-发车时刻-到达站

 车次号在 12306 真实数据里**不唯一**：同一段会返回同号不同终到站的车
    （如 G1308 08:36 分别终到「石家庄」与「正定机场」两趟）。若用 trainNo 当身份，
    前端两趟会被同时勾选（同 key），后端回写也只会取到第一趟（到达站/票价全错）。
    故凡涉及"哪一趟"的判定（选中、校验、回写）一律用本标识。
    """
    return f"{row.get('code')}-{row.get('dep')}-{row.get('to')}"


def _rows_to_options(rows: list[dict], prefer_seat: str, limit: int) -> list[TrainOption]:
    """12306 原始行 → TrainOption：按发车时间升序取前 limit 趟（仅排序取数，不做时间校验）"""
    options: list[TrainOption] = []
    for row in sorted(rows, key=lambda r: r.get("dep") or ""):
        picked = _pick_seat(row.get("seats") or {}, prefer_seat)
        if not picked:                       # 无席别数据（多为已售罄/未返回票价）则跳过该趟
            continue
        seat_name, (status, price) = picked
        options.append(TrainOption(
            trainNo=row["code"], startTime=row["dep"], endTime=row["arr"],
            startStation=row["from"], endStation=row["to"], duration=row["dur"],
            price=float(price), seatType=seat_name, ticketStatus=_ticket_status(status),
            option_id=option_id_of(row),
        ))
        if len(options) >= limit:
            break
    return options

async def _query_one_leg(
    query_tool: BaseTool, segment_id: str, leg: TrainLeg, prefer_seat: str, limit: int
) -> TrainSegment:
    """查询单段班次（由 asyncio.gather 并行调度）；任何异常都降级为空 train_list，绝不抛出"""
    travel_date = (leg.travel_date or "").strip()
    try:
        raw = await query_tool.ainvoke({
            "date": travel_date, "fromStation": leg.from_, "toStation": leg.to,
        })
    except Exception as exc:                 # 单段失败不影响其它段，前端对空段置灰
        app_logger.warning(f"⚠️ train_multi_segment {segment_id}（{leg.from_}→{leg.to}）查询异常：{exc}")
        return TrainSegment(
            segment_id=segment_id, from_=leg.from_, to=leg.to,
            date=travel_date, train_list=[],
        )
    # ★ 原始返回解析不出，再试紧凑行（工具可能已被守卫包装过）
    rows = parse_ticket_rows(raw) or _compact_to_rows(raw)
    options = _rows_to_options(rows, prefer_seat, limit)
    app_logger.info(
        f"🚄 train_multi_segment {segment_id} {leg.from_}→{leg.to} {travel_date} "
        f"解析 {len(rows)} 趟 → 取 {len(options)} 趟"
    )
    return TrainSegment(
        segment_id=segment_id, from_=leg.from_, to=leg.to,
        date=travel_date, train_list=options,
    )


async def build_train_multi_segment_payload(
    legs: list[Any],
    seat_type: str = "二等座",
    max_options_per_leg: int = 10,
    cache_key: Optional[str] = None,
) -> TrainMultiSegmentPayload:
    """核心函数（非工具）：并行查询全部段 → 组装 payload，并缓存供提交侧校验

    legs 长度即段数（2/3/4 段均支持，不写死）；日期为空时原样留空由前端展示，
    不推断时序、不校验时间先后。
    cache_key：隔离键（调用方传 conversation_id），保证提交侧校验到的是**本会话**的候选表
    """
    try:
        parsed = [leg if isinstance(leg, TrainLeg) else TrainLeg.model_validate(leg) for leg in legs]
    except Exception as exc:
        app_logger.warning(f"⚠️ train_multi_segment 入参不合法，已降级为空 payload：{exc}")
        return TrainMultiSegmentPayload(segments=[])
    if not parsed:
        return TrainMultiSegmentPayload(segments=[])
    query_tool = await _resolve_query_tool()
    if query_tool is None:                   # 数据源不可用：返回空段而不是抛错，避免打断对话
        empty = TrainMultiSegmentPayload(segments=[
            TrainSegment(segment_id=f"s{i}", from_=leg.from_, to=leg.to,
                         date=(leg.travel_date or "").strip(), train_list=[])
            for i, leg in enumerate(parsed, 1)
        ])
        _PAYLOAD_CACHE.store(empty, cache_key)
        return empty
    # ★ 全部段并行查询：总耗时 ≈ 最慢的那一段
    segments = await asyncio.gather(*[     # ← 并行并发，非串行逐段
        _query_one_leg(query_tool, f"s{index}", leg, seat_type, max_options_per_leg)
        for index, leg in enumerate(parsed, 1)
    ])
    payload = TrainMultiSegmentPayload(segments=list(segments))
    _PAYLOAD_CACHE.store(payload, cache_key)  # 供提交工具做「是否存在」校验
    return payload


# ==================== 提交侧：只做基础合法性校验（明确不做时间校验） ====================

def validate_train_submission(
    payload: TrainSubmitPayload, cache_key: Optional[str] = None
) -> TrainSubmitResult:
    """校验 segment_id 是否存在、option_id 是否在该段候选列表内；不比对任何时间先后"""
    cached = get_cached_payload(cache_key)
    if cached is None:                        # 没查过班次就提交：拦下，避免写进不存在的车次
        return TrainSubmitResult(
            ok=False, code="NO_PAYLOAD",
            msg="尚无候选班次记录，请先获取班次卡片后再提交",
        )
    items = payload.segments
    if not items:
        return TrainSubmitResult(
            ok=False, code="EMPTY_SELECTION", msg="提交内容为空，至少要选择一段车次",
        )
    # 身份校验用 option_id（车次号在同段可重复，不能当身份）：缺 option_id 一律拦下要求重选，
    # 绝不用 trainNo 兜底匹配，避免静默写错车次
    no_id = [f"{item.segment_id}·{item.trainNo or '未填写车次号'}"
             for item in items if not (item.option_id or "").strip()]
    if no_id:
        return TrainSubmitResult(
            ok=False, code="MISSING_OPTION_ID",
            msg=f"以下选择缺少车次标识（option_id）：{'、'.join(no_id)}；"
                f"请在卡片中重新选择后提交",
        )
    by_id = {seg.segment_id: seg for seg in cached.segments}
    unknown = [item.segment_id for item in items if item.segment_id not in by_id]
    if unknown:
        return TrainSubmitResult(
            ok=False, code="SEGMENT_NOT_FOUND",
            msg=f"不存在的行程段：{'、'.join(unknown)}；合法段为 {'、'.join(by_id)}",
        )
    bad_train = [
        f"{item.segment_id}·{item.option_id}" for item in items
        if item.option_id not in {t.option_id for t in by_id[item.segment_id].train_list}
    ]
    if bad_train:
        return TrainSubmitResult(
            ok=False, code="TRAIN_NOT_FOUND",
            msg=f"以下车次不在该段候选列表中：{'、'.join(bad_train)}",
        )
    chosen = {item.segment_id for item in items}
    # ★ 实时无候选（train_list 为空/查询失败、卡片里置灰）的段不算"漏选"——否则该段无从选择、
    #   用户永远凑不齐全部段而卡在交通步。只有"有候选却没选"的段才判 INCOMPLETE。
    missing = [seg.segment_id for seg in cached.segments
               if seg.segment_id not in chosen and seg.train_list]
    if missing:
        return TrainSubmitResult(
            ok=False, code="INCOMPLETE",
            msg=f"以下行程段尚未选择车次：{'、'.join(missing)}",
        )
    return TrainSubmitResult(
        ok=True, code="OK",
        msg=f"已收到 {len(items)} 段选中车次，基础合法性校验通过（未做任何时间校验）",
        accepted=items,
    )


# ==================== Agent 可调用的两个 Tool ====================

@tool
async def query_train_multi_segment_tool(
    legs: list[dict],
    seat_type: str = "二等座",
    max_options_per_leg: int = 5,
) -> str:
    """并行查询多段高铁班次，返回可直接下发前端的 train_multi_segment skill payload。

    适用于「多目的地闭环行程」需要一次性给出各段候选车次的场景
    （如 石家庄→北京→上海→石家庄）。段与段之间**并发查询**，总耗时≈最慢一段，
    段数由 legs 长度决定（2/3/4 段均可），不是串行逐段查。

    Args:
        legs: 行程段列表，按时间顺序排列。每项含 from（出发城市）、to（到达城市）、
            travel_date（该段乘车日期 YYYY-MM-DD）；闭环行程最后一段回到起点。
        seat_type: 期望席别，默认二等座。
        max_options_per_leg: 每段返回的车次上限，默认 5。

    Returns:
        JSON 文本 {"skill_name":"train_multi_segment","segments":[...]}。
        **请原样下发给前端 UI Skill**：不要改写字段名、不要增删 segments、不要自己再校验时间；
        某些段查不到班次时该段 train_list 为空数组，如实告知用户即可。
    """
    payload = await build_train_multi_segment_payload(legs, seat_type, max_options_per_leg)
    app_logger.info(f"🚄 train_multi_segment 出参：{len(payload.segments)} 段")
    return payload.model_dump_json(by_alias=True)


@tool
async def submit_train_multi_segment_tool(segments: list[dict]) -> str:
    """接收前端一次性回传的全部段选中车次，只做基础合法性校验。

    **不做任何列车时间校验**（不比对到站/发车先后、不判断耗时是否够中转）。

    Args:
        segments: 前端 onSubmit 抛回的数组，每项为
            {"segment_id": "s1", "option_id": "G1308-08:36-正定机场", "trainNo": "G1308"}。
            **option_id 是身份**（车次号在同段可能有多趟，不能当身份）。

    Returns:
        JSON 文本 {"ok": bool, "code": str, "msg": str, "accepted": [...]}。
        ok 为 false 时把 msg 原样告知用户，并请其在界面上改选后重新提交；
        ok 为 true 表示已确认全部段选择（未锁票、未做时间校验）。
    """
    try:
        payload = TrainSubmitPayload.model_validate({"segments": segments})
    except Exception as exc:                  # 结构缺失（如没有 trainNo）也走确定性回执，不抛错
        return TrainSubmitResult(
            ok=False, code="INVALID_PAYLOAD", msg=f"提交数据结构不合法：{exc}"
        ).model_dump_json()
    result = validate_train_submission(payload)
    app_logger.info(f"🚄 train_multi_segment 提交校验：{result.code} / {result.msg}")
    return result.model_dump_json()


__all__ = [
    "TRAIN_MULTI_SEGMENT_SKILL",
    "build_train_multi_segment_payload",
    "get_cached_payload",
    "train_score",
    "validate_train_submission",
    "query_train_multi_segment_tool",
    "submit_train_multi_segment_tool",
]