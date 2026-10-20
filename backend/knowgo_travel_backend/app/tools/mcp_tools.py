"""
MCP 工具筛选器
按需获取特定类型的 MCP 工具
"""
import ast
import contextvars
import json
import re
from datetime import date as _date
from typing import Any, List
from langchain_core.tools import BaseTool
from app.mcp_core.client import get_mcp_client
from app.utils.logger import app_logger


# ===== 工具入参代码层兜底（准则：不允许只靠提示词保证参数合法） =====
# 实测模型调 get-tickets 时漏传 fromStation/toStation、或把日期写成 trainDate，12306 直接
# -32602 拒绝 → 该段降级"实时班次暂不可用"。prompt 里写参数名不可靠（用户明确准则），
# 故在工具层做确定性处理：① 别名归一 ② 用本轮上下文回填 ③ 仍缺则返回可读提示。
# 上下文用 contextvar（按请求任务隔离，不跨会话污染）；由 middleware 每轮写入。
_TURN_CTX: contextvars.ContextVar[dict] = contextvars.ContextVar("tool_arg_ctx", default={})

_TRAIN_ARG_ALIAS = {
    # 日期别名
    "trainDate": "date", "departureDate": "date", "travelDate": "date",
    "dateStr": "date", "startDate": "date", "departureTime": "date",
    # 出发站别名
    "from": "fromStation", "fromStationName": "fromStation", "startStation": "fromStation",
    "fromCity": "fromStation", "departure": "fromStation", "departureStation": "fromStation",
    # 到达站别名
    "to": "toStation", "toStationName": "toStation", "endStation": "toStation",
    "toCity": "toStation", "arrival": "toStation", "arrivalStation": "toStation",
}


# ★ 中间件每轮写一次：本轮已知的出发地 / 目的地 / 日期 / 当前段，供车票守卫回填入参
def set_turn_context(
        departure_city: str = "", destination: str = "", date: str = "",
        multi_leg: bool = False, leg_from: str = "", leg_to: str = ""
) -> None:
    """由中间件每轮写入本轮已知信息（出发地/目的地/出发日期/是否多段/当前段起），供工具入参回填"""
    _TURN_CTX.set({
        "departure_city": departure_city or "",
        "destination": destination or "",
        "date": date or "",
        "multi_leg": bool(multi_leg),
        # 多目的地段级循环下，"当前待确认段"的起讫站是参数的权威来源——
        # 由段游标（代码）给出，模型漏传时按此回填，保证"一次只查当前段"
        "leg_from": leg_from or "",
        "leg_to": leg_to or "",
    })


def _normalize_train_args(kwargs: dict) -> dict | str:
    """交通查询入参归一：别名纠正 → 缺失回填 → 仍缺返回可读提示（不再抛底层 MCP 报错）"""
    args = dict(kwargs or {})
    for k in list(args):
        canon = _TRAIN_ARG_ALIAS.get(k)
        if not canon:
            continue
        if not args.get(canon):
            args[canon] = args[k]
        args.pop(k, None)               # 别名键必须移除（服务端 schema 禁止多余字段）
    ctx = _TURN_CTX.get() or {}
    if not args.get("date") and ctx.get("date"):
        args["date"] = ctx["date"]
    if not ctx.get("multi_leg"):        # 多段时严禁猜测起讫站，否则会查到错误路段
        if not args.get("fromStation") and ctx.get("departure_city"):
            args["fromStation"] = ctx["departure_city"]
        if not args.get("toStation") and ctx.get("destination"):
            args["toStation"] = ctx["destination"]
    else:
        # 多段模式下**日期也按段定**，模型传的日期一律覆盖——实测它对三段
        # 城际全填出发日（09-18），导致"同一天既在重庆又在北京"的荒谬时序；段日期由中间件
        # 按城市天数分配算出，此处只认上下文里的权威值
        if ctx.get("date"):
            args["date"] = ctx["date"]
        # 多段模式改为"按段游标回填当前段"——不再要求模型补全（它补的往往是
        # 编造的段），只要中间件写入了当前段起讫站，就补齐缺失站名，确保查的是正在确认的那一段
        if not args.get("fromStation") and ctx.get("leg_from"):
            args["fromStation"] = ctx["leg_from"]
        if not args.get("toStation") and ctx.get("leg_to"):
            args["toStation"] = ctx["leg_to"]
    missing = [k for k in ("date", "fromStation", "toStation") if not args.get(k)]
    if missing:
        return (
            f"【调用参数不完整】缺少 {' / '.join(missing)}。请补全后重新调用本工具："
            "fromStation=出发站中文名，toStation=到达站中文名，date=YYYY-MM-DD。"
        )
    # 日期必须不是过去——实测模型（Qwen）日期感错乱，在需求收集轮自行
    # 调用 get-tickets 时传了过去的日期，12306 直接拒绝，用户看到"查询时出现了一个错误，
    # 看起来是由于选择了过去的日期"。提示词约束不住（用户准则），此处做确定性拦截：
    # 过去日期一律不发给服务端，也不猜测替代值（改动用户日期更危险），交由模型向用户确认
    try:
        if _date.fromisoformat(str(args["date"])[:10]) < _date.today():
            app_logger.warning(f"⚠️ {args['date']} 是过去的日期，已拦下（12306 不查历史班次）")
            return (f"【日期无效】{args['date']} 已是过去的日期，12306 只能查询今天及之后的班次。"
                    "请先向用户确认出发日期，再用今天或之后的日期（YYYY-MM-DD）重新调用。")
    except ValueError:
        pass
    return args

# ===== 车票结果代码层裁剪（防"4.5 万字淹没"）=====
# 实测 12306 单次返回 287 趟/45,633 字，模型被淹没后编造出 G1001/G1003/G1005（¥154.5，
# 真实数据里不存在）。提示词约束不住数据量，故在工具层做确定性裁剪：解析真实班次 →
# 按发车时间排序 → 只保留前 N 趟 → 一行一趟（含席别票价）。模型只看到十几行，无从淹没。
_TRAIN_ROW_RE = re.compile(
    r"^([A-Z]\d{1,4})\s+(.+?)\(telecode:[^)]*\)\s*->\s*(.+?)\(telecode:[^)]*\)\s+"
    r"(\d{2}:\d{2})\s*->\s*(\d{2}:\d{2})\s+历时：(\d{1,2}:\d{2})",
    re.MULTILINE,
)
_SEAT_RE = re.compile(r"^-\s*([^:：]+)[:：]\s*(\S+)\s+([\d.]+)元", re.MULTILINE)
# 裁剪后的紧凑行：车次|出发→到达|出发→到达时刻|历时|席别票价（同时供 chat 层回读校验）
_COMPACT_ROW_RE = re.compile(
    r"^([A-Z]\d{1,4})\|([^|\n]+?)→([^|\n]+?)\|(\d{2}:\d{2})→(\d{2}:\d{2})\|"
    r"(\d{1,2}:\d{2})\|([^|\n]+)$",
    re.MULTILINE,
)
_SEAT_ORDER = ("二等座", "硬座", "一等座", "硬卧", "软卧", "无座")
_TICKET_LIMIT = 15


def _raw_text(raw: Any) -> str:
    """MCP 返回可能是 content blocks 列表，统一取纯文本"""
    if isinstance(raw, list):
        return "\n".join(b.get("text", "") for b in raw if isinstance(b, dict))
    return str(raw or "")


def parse_ticket_rows(raw: Any) -> list[dict]:
    """解析 12306 原始返回：每趟车一条记录（车次/区间/时刻/历时/各席别票价）"""
    text = _raw_text(raw)
    rows: list[dict] = []
    hits = list(_TRAIN_ROW_RE.finditer(text))
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
        seats = {
            s.group(1).strip(): (s.group(2).strip(), float(s.group(3)))
            for s in _SEAT_RE.finditer(text[m.end():end])
        }
        rows.append({
            "code": m.group(1), "from": m.group(2).strip(), "to": m.group(3).strip(),
            "dep": m.group(4), "arr": m.group(5), "dur": m.group(6), "seats": seats,
        })
    return rows


def parse_compact_rows(text: Any) -> list[dict]:
    """解析裁剪后的紧凑班次行（供页面展示层回读真实数据做权威清单）"""
    return [
        {"code": m.group(1), "from": m.group(2), "to": m.group(3), "dep": m.group(4),
         "arr": m.group(5), "dur": m.group(6), "seat": m.group(7)}
        for m in _COMPACT_ROW_RE.finditer(_raw_text(text))
    ]


def compact_ticket_text(raw: Any, limit: int = _TICKET_LIMIT) -> str:
    """把车票查询结果压成"一行一趟"（按发车时间升序取前 limit 趟）；解析失败则截断降级"""
    rows = parse_ticket_rows(raw)
    if not rows:
        return _raw_text(raw)[:2000]
    rows.sort(key=lambda r: r["dep"])
    shown = rows[:limit]
    lines = [
        f"实时班次（共 {len(rows)} 趟，按发车时间排序，仅列前 {len(shown)} 趟）：",
        "格式：车次|区间|出发→到达|历时|票价",
    ]
    for r in shown:
        seat = next((k for k in _SEAT_ORDER if k in r["seats"]), None)
        price = (
            f"{seat} {r['seats'][seat][0]} {r['seats'][seat][1]:g}元"
            if seat else "票价未返回"
        )
        lines.append(f"{r['code']}|{r['from']}→{r['to']}|{r['dep']}→{r['arr']}|{r['dur']}|{price}")
    app_logger.info(
        f"✂️ 车票结果裁剪：原始 {len(_raw_text(raw))} 字/{len(rows)} 趟 → "
        f"压缩 {len(chr(10).join(lines))} 字/{len(shown)} 趟"
    )
    return "\n".join(lines)


class _ArgGuardTool(BaseTool):
    """ 入参兜底包装（name/description/args_schema 原样透传，仅拦截调用入参）"""
    name: str
    description: str
    inner: Any = None

    def _run(self, *args, **kwargs):
        raise NotImplementedError("仅支持异步调用")

    async def _arun(self, **kwargs):
        args = _normalize_train_args(kwargs)
        if isinstance(args, str):
            app_logger.warning(f"⚠️ {self.name} 入参不完整，已拦下并提示补全：{kwargs}")
            return args
        # ★ 只拦截入参：纠正、回填之后交给原工具去查
        result = await self.inner.ainvoke(args)
        # 结果裁剪后再交给模型（防海量班次淹没上下文诱发编造）
        # ★ 结果顺手压成"一行一趟"：模型看得清，上下文也不会被撑爆
        return compact_ticket_text(result)


def _guard_train_tool(tool: BaseTool) -> BaseTool:
    """仅包装"必填 date/fromStation/toStation"的车票查询工具（按 schema 判定，不写死工具名）"""
    schema = tool.args_schema if isinstance(tool.args_schema, dict) else {}
    # ★ 按 schema 认车票工具，不写死工具名
    if not {"date", "fromStation", "toStation"} <= set(schema.get("required") or []):
        return tool
    return _ArgGuardTool(
        name=tool.name, description=tool.description,
        args_schema=tool.args_schema, inner=tool,
    )


def apply_train_arg_guard(tools: List[BaseTool]) -> List[BaseTool]:
    """ 对全量工具套用车票守卫（注册侧同源，供 create_agent 使用）

    A 的包装此前只作用于 get_train_tools() 的返回值——那是 middleware 注入给模型
    "看"的一份；而 LangGraph 的 ToolNode 是按工具名到 create_agent(tools=...) 的注册表里取对象
    **执行**的，那里注册的是 get_all_mcp_tools() 的原始工具。两者不是同一批对象，
    导致参数兜底与结果裁剪在真实跑图时被整体绕过（实测：跑图时 ToolMessage 是 39,265 字
    12306 原文、日志无"车票结果裁剪"，并因此撑爆模型 30720 输入上限）。
    """
    return [_guard_train_tool(t) for t in tools]


# ===== 航班直查所需的机场码表与结果解析 =====
# searchFlightsByDepArr 的 dep/arr 必须是**机场**三字码：实测 dep=BJS 返回"暂无数据"、
# dep=PEK 正常（75k 字/多条），故不能复用按城市码的工具口径。未收录城市不放行直查。
_CITY_AIRPORT = {
    "北京": "PEK", "上海": "SHA", "广州": "CAN", "深圳": "SZX", "成都": "CTU",
    "杭州": "HGH", "武汉": "WUH", "西安": "XIY", "南京": "NKG", "重庆": "CKG",
    "长沙": "CSX", "厦门": "XMN", "福州": "FOC", "青岛": "TAO", "昆明": "KMG",
    "天津": "TSN", "郑州": "CGO", "哈尔滨": "HRB", "沈阳": "SHE", "大连": "DLC",
    "三亚": "SYX", "海口": "HAK", "桂林": "KWL", "贵阳": "KWE", "南宁": "NNG",
    "丽江": "LJG", "大理": "DLU", "西双版纳": "JHG", "张家界": "DYG", "拉萨": "LXA",
    "乌鲁木齐": "URC", "喀什": "KHG", "敦煌": "DNH", "呼和浩特": "HET", "银川": "INC",
    "西宁": "XNN", "兰州": "LHW", "太原": "TYN", "大同": "DAT", "石家庄": "SJW",
    "秦皇岛": "BPE", "济南": "TNA", "烟台": "YNT", "威海": "WEH", "临沂": "LYI",
    "潍坊": "WEF", "合肥": "HFE", "黄山": "TXN", "南昌": "KHN", "赣州": "KOW",
    "温州": "WNZ", "宁波": "NGB", "舟山": "HSN", "台州": "HYN", "义乌": "YIW",
    "无锡": "WUX", "常州": "CZX", "南通": "NTG", "扬州": "YTY", "徐州": "XUZ",
    "珠海": "ZUH", "佛山": "FUO", "惠州": "HUZ", "汕头": "SWA", "梅州": "MXZ",
    "湛江": "ZHA", "北海": "BHY", "泉州": "JJN", "武夷山": "WUS", "洛阳": "LYA",
    "宜昌": "YIH", "衡阳": "HNY", "怀化": "HJJ", "九寨沟": "JZH", "绵阳": "MIG",
    "长春": "CGQ", "延吉": "YNJ", "包头": "BAV", "鄂尔多斯": "DSN",
}

# ★ 票价工具（getFlightPriceByCities）要的是**城市**三字码，与上面的**机场**码不是一回事：
#   北京 城市码 BJS / 机场码 PEK、上海 城市码 SHA（恰好同名）/ 机场码 SHA|PVG。
#   两张表必须分开维护，混用会让票价查询整段查空。
_CITY_IATA = {
    "北京": "BJS", "上海": "SHA", "广州": "CAN", "深圳": "SZX", "成都": "CTU",
    "杭州": "HGH", "武汉": "WUH", "西安": "SIA", "南京": "NKG", "重庆": "CKG",
    "长沙": "CSX", "厦门": "XMN", "福州": "FOC", "青岛": "TAO", "昆明": "KMG",
    "天津": "TSN", "郑州": "CGO", "哈尔滨": "HRB", "沈阳": "SHE", "大连": "DLC",
    "海口": "HAK", "三亚": "SYX", "贵阳": "KWE", "济南": "TNA", "合肥": "HFE",
    "南昌": "KHN", "南宁": "NNG", "太原": "TYN", "兰州": "LHW", "乌鲁木齐": "URC",
}

# ★ 票价字段候选名：不同接口/不同版本叫法不一（实测同一家 MCP 换版本就改名），
#   故按候选表逐个试，谁先命中用谁；**一个都没命中就留空，绝不编造**。
_FLIGHT_PRICE_KEYS = (
    "FlightYPrice", "FlightPrice", "LowestPrice", "MinPrice", "TicketPrice",
    "CabinPrice", "Price", "lowestPrice", "minPrice", "ticketPrice", "price",
)
# 航班号形态：两位航司码 + 2~4 位数字（CA1501 / MU5104 / 9C8891）
_FLIGHT_NO_RE = re.compile(r"\b([A-Z0-9]{2}\d{2,4})\b")
# 合理票价区间：低于 80 元多半是"燃油费/机建费"等零头，高于 20000 多半是里程/积分，
# 两头都不收，避免把非票价数字当票价写进卡片
_PRICE_MIN, _PRICE_MAX = 80.0, 20000.0


def _sane_price(value: Any) -> float:
    """宽松取票价并做区间过滤；不合理或转不出一律返回 0（调用方按"价格未知"处理）"""
    try:
        num = float(re.sub(r"[^\d.]", "", str(value)) or 0)
    except (TypeError, ValueError):
        return 0.0
    return num if _PRICE_MIN <= num <= _PRICE_MAX else 0.0


def parse_flight_rows(raw: Any) -> list[dict]:
    """解析航班查询结果（VariFlight 返回的是 Python dict 文本，非严格 JSON）：每班一条

    取值两级（与 parse_flight_prices 同口径）：① 严格 JSON → ② 文本里的 dict 字面量
    （单引号 repr，json.loads 解析不了、ast.literal_eval 可以）；两级都取不到即返回空表。
    error_code=10（暂无数据）返回空表，由调用方给出确定性兜底话术

    ★ price：searchFlightsByDepArr 是**航班动态**接口，多数航线不返回票价，此处按
    _FLIGHT_PRICE_KEYS 候选键尽力抠一次，抠不到记 0，由上层再去问票价接口补。
    """
    text = _raw_text(raw)

    def _row(rec: dict) -> dict | None:
        """从一条航班记录 dict 按字段名取值；缺航班号/时刻返回 None（与旧口径一致）"""
        code = str(rec.get("FlightNo") or "").strip()
        dep_at = str(rec.get("FlightDeptimePlanDate") or "")
        if not code or len(dep_at) < 16:
            return None
        price = 0.0
        for key in _FLIGHT_PRICE_KEYS:            # ★ 抠不到就是 0，绝不编造
            if rec.get(key) not in (None, ""):
                price = _sane_price(rec[key])
                if price:
                    break
        return {
            "code": code, "airline": re.sub(r"(股份)?有限公司$", "", str(rec.get("FlightCompany") or "")),
            "from": str(rec.get("FlightDepAirport") or rec.get("FlightDep") or ""),
            "to": str(rec.get("FlightArrAirport") or rec.get("FlightArr") or ""),
            "date": dep_at[:10], "dep": dep_at[11:16],
            "arr": str(rec.get("FlightArrtimePlanDate") or "")[11:16],
            "terminal": str(rec.get("FlightHTerminal") or ""), "ontime": str(rec.get("OntimeRate") or ""),
            "stop": str(rec.get("StopCity") or ""), "state": str(rec.get("FlightState") or ""),
            "price": price,
        }

    # ① JSON / ② 文本里的 Python 字面量：都取 data 列表，逐条按字段名取值
    obj = _loads_first_json(text)
    if obj is None:
        start = text.find("{")
        try:
            obj = ast.literal_eval(text[start:]) if start != -1 else None
        except (ValueError, SyntaxError):
            obj = None
    recs = obj.get("data") if isinstance(obj, dict) else obj
    rows: list[dict] = []
    for rec in (recs if isinstance(recs, list) else []):
        if isinstance(rec, dict):
            row = _row(rec)
            if row:
                rows.append(row)
    uniq: dict[tuple, dict] = {}
    for r in sorted(rows, key=lambda x: x["dep"]):   # 同时刻同号只留一条
        # ★ 同航班号、同起飞时刻只留一条
        uniq.setdefault((r["code"], r["dep"]), r)
    return sorted(uniq.values(), key=lambda x: x["dep"])



def parse_flight_prices(raw: Any) -> tuple[dict[str, float], float]:
    """解析票价接口返回 → (按航班号的票价表, 该航线最低价)

    票价接口的返回形态实测有两种，两种都要认（与 parse_driving_rows 的三段式同口径）：
      ① 严格 JSON：[{"flightNo": "CA1501", "price": 1280}] 或 {"data": [...]} 嵌一层
      ② VariFlight 舱位形态（实测主力）：每班价格在 cabins 里按舱位给出，只认经济舱

    返回值两项各有用途：
      by_flight —— 能对上航班号的票价，直接写进对应候选航班的 price
      lowest    —— 该航线最低价（按航班号表的最小值）；
                   **绝不**把它当成每一班的票价挨个填（那就是编造）
    任何解析不出的情况一律返回 ({}, 0.0)，调用方按"价格未知"处理。
    """
    text = _raw_text(raw[0] if isinstance(raw, tuple) and raw else raw)
    if not text:
        return {}, 0.0
    by_flight: dict[str, float] = {}

    def _walk(node: Any) -> None:
        """递归遍历 JSON：遇到同时含"航班号 + 票价"的 dict 就收录（取同号最低价）"""
        if isinstance(node, list):
            for item in node:
                _walk(item)
            return
        if not isinstance(node, dict):
            return
        no = ""
        for key in ("flightNo", "FlightNo", "flightNumber", "flight_no", "fnum", "no"):
            if str(node.get(key) or "").strip():
                no = str(node[key]).strip().upper()
                break
        price = 0.0
        for key in _FLIGHT_PRICE_KEYS:
            if node.get(key) not in (None, ""):
                price = _sane_price(node[key])
                if price:
                    break
        if no and price and _FLIGHT_NO_RE.fullmatch(no):
            by_flight[no] = min(by_flight.get(no, price), price)
        for value in node.values():          # 继续往下钻（data/list/flights 等嵌套层）
            if isinstance(value, (list, dict)):
                _walk(value)

    _walk(_loads_first_json(text))

    if not by_flight:                        # ② VariFlight 舱位形态：剥前缀后按 Python 字面量取值
        # ★ 原始返回是 "Flight prices: {'code': 200, 'data': [{'flightno': 'MU5160', …,
        #   'cabins': [{'cabinclass': 'Y', …, 'price': 665}]}]}"（单引号 repr，非 JSON）——
        #   json.loads 解析不了，故从首个 '{' 起 ast.literal_eval，再按字段名取号/取价；
        start = text.find("{")
        try:
            obj = ast.literal_eval(text[start:]) if start != -1 else None
        except (ValueError, SyntaxError):
            obj = None
        recs = obj.get("data") if isinstance(obj, dict) else obj
        for rec in (recs if isinstance(recs, list) else []):
            if not isinstance(rec, dict):
                continue
            no = str(rec.get("flightno") or "").upper()
            # 只认经济舱（cabinclass='Y'）的 'price' 并取最低；无经济舱价的班次留空，不拿公务舱充数
            econ = [_sane_price(c.get("price")) for c in (rec.get("cabins") or [])
                    if isinstance(c, dict) and str(c.get("cabinclass") or "").upper() == "Y"]
            econ = [p for p in econ if p]
            if no and econ:
                price = min(econ)
                by_flight[no] = min(by_flight.get(no, price), price)

    # 航线级最低价：按航班号表的最小值；表为空就是 0（调用方按"价格未知"处理）
    lowest = min(by_flight.values()) if by_flight else 0.0
    return by_flight, lowest


def flight_price_args(tool: Any, date: str, dep_city: str, arr_city: str) -> dict:
    """按票价工具**自己声明的**入参名组装入参（没声明到的名字不猜、不传）

    ★ 为什么需要它：票价接口的入参名各家/各版本不一——线上核对 schema 是
      dep_city/arr_city/dep_date，而项目内的演示脚本按 depCityCode/arrCityCode/depDate 写。
      写死一套名字换个版本就整段查空，故按关键词归类。
      直查通道（chat_stream_executor）与快速通道（express_planning_service）都调这里；
      航班卡片另有同源实现 flight_agent_tool._price_args（多一步必填项校验）。
    传进来的两个码是**城市码**（北京 BJS），不是机场码（北京 PEK）——两套码不能混用。
    """
    schema = tool.args_schema if isinstance(tool.args_schema, dict) else {}
    args: dict = {}
    for name in (schema.get("properties") or {}):
        low = name.lower()
        # ★ 先判到达：arrCity/destCity 里也可能含 "to"，顺序颠倒会把航线查反
        if "date" in low:
            args[name] = date
        elif "arr" in low or "dest" in low or low in ("to", "dst"):
            args[name] = arr_city
        elif "dep" in low or "from" in low or "orig" in low or low in ("src",):
            args[name] = dep_city
    return args


def _loads_first_json(text: str) -> Any:
    """先整体 json.loads；失败则扫出首个括号配平的 {...} 再解析；都没有返回 None"""
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    while start != -1:
        depth = 0
        for end in range(start, len(text)):
            if text[end] == "{":
                depth += 1
            elif text[end] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:end + 1])
                    except Exception:
                        break
        start = text.find("{", start + 1)
    return None

# ===== 多段自驾所需的高德驾车结果解析 =====
# maps_direction_driving 的 origin/destination 要"经度,纬度"，已收录城市用本表中心坐标；
# 未收录城市由用车现场退回城市名照样尝试（高德不接受即查询失败、降级空段，绝不编造坐标）
_CITY_COORD = {
    "北京": "116.407,39.904", "上海": "121.474,31.230", "广州": "113.264,23.129",
    "深圳": "114.057,22.543", "成都": "104.066,30.572", "杭州": "120.155,30.274",
    "武汉": "114.305,30.593", "西安": "108.940,34.341", "南京": "118.796,32.060",
    "重庆": "106.551,29.563", "长沙": "112.938,28.228", "厦门": "118.089,24.479",
    "福州": "119.296,26.074", "青岛": "120.383,36.067", "昆明": "102.833,24.880",
    "天津": "117.190,39.125", "郑州": "113.625,34.746", "哈尔滨": "126.535,45.803",
    "沈阳": "123.431,41.805", "大连": "121.615,38.914", "海口": "110.199,20.044",
    "三亚": "109.508,18.247", "贵阳": "106.630,26.647", "济南": "117.121,36.651",
    "合肥": "117.227,31.821", "南昌": "115.858,28.682", "南宁": "108.366,22.817",
    "太原": "112.549,37.857", "兰州": "103.834,36.061", "乌鲁木齐": "87.617,43.793",
    "呼和浩特": "111.749,40.842", "石家庄": "114.514,38.042", "银川": "106.231,38.487",
    "西宁": "101.778,36.617", "拉萨": "91.140,29.645", "苏州": "120.585,31.299",
    "无锡": "120.312,31.491", "宁波": "121.550,29.875", "温州": "120.699,27.994",
    "珠海": "113.577,22.271", "桂林": "110.290,25.274", "丽江": "100.230,26.872",
    "张家界": "110.479,29.117", "敦煌": "94.662,40.142",
}
_DRIVING_SUMMARY_LIMIT = 3   # 路段摘要最多取前 3 条 step 指令（防正文膨胀）


def _to_float(value: Any) -> float | None:
    """宽松取数：字符串/数字都能转；转不了返回 None（绝不返回编造值）"""
    if value is None or value == "":
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _seconds_to_hhmm(seconds: Any) -> str:
    """高德 duration 单位为秒 → HH:mm 文本；非法/非正数返回空串（不猜测）"""
    val = _to_float(seconds)
    if val is None or val <= 0:
        return ""
    minutes = int(val) // 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _driving_summary(path: dict, text: str) -> str:
    """路段摘要：优先取 steps 的 instruction/road_name 前几条；解析不到再从原文正则兜底"""
    parts: list[str] = []
    steps = path.get("steps") if isinstance(path, dict) else None
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict):
                continue
            ins = str(step.get("instruction") or step.get("road_name") or "").strip()
            if ins:
                parts.append(ins)
            if len(parts) >= _DRIVING_SUMMARY_LIMIT:
                break
    if not parts:                                   # 结构化路径没取到 → 原文正则兜底
        parts = re.findall(r'"instruction"\s*:\s*"([^"]{1,60})"', text)[:_DRIVING_SUMMARY_LIMIT]
    return "；".join(parts)[:120]


def parse_driving_rows(raw: Any) -> dict:
    """解析 maps_direction_driving 返回的**默认策略首条**路线（只读确认，不做策略选择）

    兼容三种返回：① content blocks 列表 ② 严格 JSON 文本 ③ 含 {...} 的纯文本。
    提取 distance（米→km）、duration（秒→HH:mm）、tolls（元）、traffic_lights 与路段摘要；
    任何字段解析不出就不放进结果，整体一个字段都没有时返回 {}（调用方降级为空段）。
    **绝不抛异常、绝不编造数字**。
    """
    text = _raw_text(raw[0] if isinstance(raw, tuple) and raw else raw)
    if not text:
        return {}
    data = _loads_first_json(text)
    path: dict = {}
    if isinstance(data, dict):
        route = data.get("route") if isinstance(data.get("route"), dict) else data
        paths = route.get("paths") if isinstance(route, dict) else None
        if isinstance(paths, list) and paths and isinstance(paths[0], dict):
            path = paths[0]                         # 默认策略的最优路线
        elif isinstance(route, dict) and "distance" in route:
            path = route

    def _pick(key: str) -> Any:
        if isinstance(path, dict) and path.get(key) not in (None, ""):
            return path.get(key)
        # 兜底：整段文本不是可解析 JSON 时，才回原文正则，一般不会触发
        m = re.search(rf'"{key}"\s*:\s*"?([0-9.]+)"?', text)   # 结构化取不到 → 原文正则
        return m.group(1) if m else None

    distance_m = _to_float(_pick("distance"))
    duration_s = _to_float(_pick("duration"))
    tolls = _to_float(_pick("tolls"))
    lights = _to_float(_pick("traffic_lights"))
    # ★ 四项一个都抠不到就返回空 dict，绝不编数字
    if distance_m is None and duration_s is None and tolls is None and lights is None:
        return {}

    out: dict = {}
    if distance_m is not None:
        out["distance_km"] = round(distance_m / 1000.0, 1)
    if duration_s is not None:
        out["duration_text"] = _seconds_to_hhmm(duration_s)
    if tolls is not None:
        out["tolls"] = tolls
    if lights is not None:
        out["traffic_lights"] = int(lights)
    summary = _driving_summary(path, text)
    if summary:
        out["summary"] = summary
    return out


async def get_all_mcp_tools() -> List[BaseTool]:
    """获取所有 MCP 工具"""
    manager = await get_mcp_client()
    tools = await manager.get_tools()
    app_logger.info(f"📦 获取了 {len(tools)} 个 MCP 工具")
    return tools


async def get_hotel_tools() -> List[BaseTool]:
    """
    获取酒店相关工具

    Returns:
        酒店搜索、周边查询等工具
    """
    manager = await get_mcp_client()
    all_tools = await manager.get_tools()

    hotel_tools = [
        tool for tool in all_tools
        if any(keyword in tool.name.lower() for keyword in [
            'find-hotels',           # 酒店搜索
            'searchhotels',          # aigohotel-mcp 实际工具名（旧关键词匹配不到，导致漏加载）
            # 不再纳入 maps_around_search——它属"周边 POI 搜索"，返回的是评分/晚
            # 的地图凑数条目，与 find-hotels 的"总价+星级+地址"格式并存时，模型会在多城住宿轮
            # 对同一城市调两种工具、各展示一批（用户实测：上海榜单一遍变两遍、标题错位）。
            # 仅保留真实酒店源，同城每次查询结果唯一、格式统一，展示干净
        ])
    ]

    app_logger.info(f"🏨 酒店工具: {[t.name for t in hotel_tools]}")
    return hotel_tools


async def get_flight_tools() -> List[BaseTool]:
    """
 获取航班相关工具（VariFlight Aviation MCP，真实数据源）
    供交通规划步骤使用，替代原 mock flight_subagent

    Returns:
        航班搜索、价格查询等工具
    """
    manager = await get_mcp_client()
    all_tools = await manager.get_tools()

    flight_tools = [
        tool for tool in all_tools
        if 'flight' in tool.name.lower()   # 覆盖 searchFlightsByDepArr / getFlightPriceByCities 等
    ]

    app_logger.info(f"✈️ 航班工具: {[t.name for t in flight_tools]}")
    return flight_tools


async def get_train_tools() -> List[BaseTool]:
    """
 获取高铁/火车相关工具（12306 MCP，真实数据源）
    供交通规划步骤使用，替代原 mock train_subagent

    Returns:
        车票查询、车次查询等工具
    """
    manager = await get_mcp_client()
    all_tools = await manager.get_tools()

    train_tools = [
        tool for tool in all_tools
        if any(keyword in tool.name.lower() for keyword in [
            'train',      # 车次/列车查询
            'ticket',     # 余票/票价查询
            'railway',    # 铁路查询（兼容不同命名风格）
        ])
    ]
    # 车票查询工具统一套入参兜底（别名归一+上下文回填+可读报错）
    train_tools = [_guard_train_tool(t) for t in train_tools]

    app_logger.info(f"🚄 高铁工具: {[t.name for t in train_tools]}")
    return train_tools


async def get_driving_tools() -> List[BaseTool]:
    """
 获取自驾路线相关工具（高德 MCP，真实数据源）
    供交通规划步骤使用，替代原 mock driving_subagent

    Returns:
        驾车路线规划工具
    """
    manager = await get_mcp_client()
    all_tools = await manager.get_tools()

    driving_tools = [
        tool for tool in all_tools
        if 'maps_direction_driving' in tool.name.lower()   # 高德驾车路线规划
    ]

    app_logger.info(f"🚗 自驾工具: {[t.name for t in driving_tools]}")
    return driving_tools


async def get_weather_tools() -> List[BaseTool]:
    """
    获取天气相关工具

    Returns:
        天气查询工具
    """
    manager = await get_mcp_client()
    all_tools = await manager.get_tools()

    weather_tools = [
        tool for tool in all_tools
        if any(keyword in tool.name.lower() for keyword in [
            'get_weather_forecast',      # 自建天气服务
        ])
    ]

    app_logger.info(f"🌤️ 天气工具: {[t.name for t in weather_tools]}")
    return weather_tools


async def get_search_tools() -> List[BaseTool]:
    """
    获取搜索相关工具

    Returns:
        旅游信息搜索工具
    """
    manager = await get_mcp_client()
    all_tools = await manager.get_tools()

    search_tools = [
        tool for tool in all_tools
        if any(keyword in tool.name.lower() for keyword in [
            'search_travel_info',  # 自建搜索服务
        ])
    ]

    app_logger.info(f"🔍 搜索工具: {[t.name for t in search_tools]}")
    return search_tools


async def get_date_tools() -> List[BaseTool]:
    """
    获取日期相关工具

    Returns:
        获取当前日期的工具
    """
    manager = await get_mcp_client()
    all_tools = await manager.get_tools()

    date_tools = [
        tool for tool in all_tools
        if any(keyword in tool.name.lower() for keyword in [
            'get-current-date',
            'gettodaydate',
        ])
    ]

    app_logger.info(f"📅 日期工具: {[t.name for t in date_tools]}")
    return date_tools