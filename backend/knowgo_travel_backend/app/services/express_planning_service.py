"""自动确认快速通道服务

针对"全程交给 AI 自动确认"的表单场景，绕过多轮对话状态机：
并行数据收集 → 规则选定 → 单次 LLM 生成行程正文 → 预算模板拼接 → 落库。

为什么：状态机为交互式引导设计，每轮 LLM 思考（8~17s）在零交互场景下是纯开销；
实测自动确认走状态机全程 2 分 31 秒，快速通道目标 50~60 秒。
手动确认模式不受影响，仍走原 handoffs 状态机。

步骤 0：需求结构化提取器——表单清单是前端拼接的文本，后端无结构化字段；
用 with_structured_output 一次 LLM（~3s）提取，auto_confirm=false 则回落状态机。
"""
from datetime import datetime, timedelta
import asyncio
import json

from pydantic import BaseModel, Field
from langchain_community.chat_models import ChatTongyi

from app.core.events import ROLE_ASSISTANT, make_event
from app.core.intent import detect_intent
from app.config import settings
from app.utils.logger import app_logger

_DIGITS = "0123456789"
def _to_float(value, default: float = 0.0) -> float:
    """安全转 float，转不了给默认值"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def _number_before(text: str, unit: str) -> float:
    """取紧挨在 unit 前面的数字，如 ('均价356CNY/晚', 'CNY/晚') → 356；没有 unit 返回 0"""
    if unit not in (text or ""):
        return 0.0
    head = text.split(unit, 1)[0].rstrip()
    i = len(head)
    while i > 0 and head[i - 1] in _DIGITS + ".":
        i -= 1
    return _to_float(head[i:])

def _hit_auto_confirm(content: str) -> bool:
    """表单里是否出现过自动确认表述（对空白不敏感）"""
    # ★ 词表在第 9 章的 app/core/intent.py，这里只留调用口（去空白后比对的规则也在那边）
    return detect_intent(content).auto_confirm


class ExpressRequirement(BaseModel):
    """快速通道需求（从表单清单文本一次提取）"""
    departure_city: str = Field(description="出发城市，未提及则为空字符串")
    destination: str = Field(description="目的地城市，未提及则为空字符串")
    departure_date: str = Field(description="出发日期 YYYY-MM-DD，相对日期须换算为具体日期")
    travel_days: int = Field(default=3, description="出行天数，未提及默认 3")
    adult_count: int = Field(default=2, description="成人数，未提及默认 2")
    children_count: int = Field(default=0, description="儿童数（<12 岁），未提及默认 0")
    budget_max: float = Field(default=0, description="人均预算上限（元），未提及为 0")
    auto_confirm: bool = Field(description="是否含『全程交给 AI 自动确认』")
    transport_types: list[str] = Field(
        default_factory=list,
        description="交通方式列表，取值 flight/train/driving，多选")
    food_preferences: list[str] = Field(
        default_factory=list,
        description="餐饮偏好列表，取值 specialty(特色美食)/chain(连锁快餐)/local(本地小吃)")
    accommodation_types: list[str] = Field(
        default_factory=list,
        description="酒店偏好列表，取值 star_hotel(舒适/高端)/economy_hotel(经济型)/hostel(民宿特色)/youth_hostel(青年旅社)")
    travel_styles: list[str] = Field(
        default_factory=list,
        description="旅行风格列表，取值 relaxation/culture/adventure/food")


_EXTRACT_PROMPT = (
    "从用户的旅行需求清单中提取结构化字段。今天的日期是 {today}。要求：\n"
    "1. 日期必须是具体 YYYY-MM-DD（相对表述如今天/下周按今天日期换算）\n"
    "2. 枚举值严格按说明取值，清单未提及的列表字段给空列表\n"
    "2.5 destination：用户提到的**所有**想去城市都保留，多城用顿号连接（如'上海、北京'）；"
    "若其中某城与出发地相同也必须保留（出发地由 departure_city 字段单独承担，"
    "**严禁**因某城是出发地就从 destination 里剔除）\n"
    "3. auto_confirm 仅当出现『全程交给 AI 自动确认/无需逐步询问』类表述时为 true\n\n"
    "需求清单：\n{content}"
)


async def extract_requirement(content: str) -> ExpressRequirement:
    """一次 LLM 调用把清单文本提取为结构化需求（~3s，快速通道的分流判定依据）"""
    llm = ChatTongyi(
        model=settings.qwen_model_name,
        api_key=settings.dashscope_api_key,
        temperature=0,
    )
    structured = llm.with_structured_output(ExpressRequirement)
    result: ExpressRequirement = await structured.ainvoke(
        _EXTRACT_PROMPT.format(
            content=content,
            today=datetime.now().strftime("%Y-%m-%d"),
        )
    )
    app_logger.info(
        f"[express] 需求提取: dest={result.destination} date={result.departure_date} "
        f"days={result.travel_days} auto={result.auto_confirm} "
        f"transport={result.transport_types} food={result.food_preferences}"
    )
    return result


async def try_express_planning(user_id: str, content: str, stream) -> ExpressRequirement | None:
    """快速通道分流判定：命中自动确认表单则返回结构化需求，否则返回 None 回落状态机。

 （用户明确要求）：**只有**"全程交给 AI 自动确认"才走快速通道。
    未勾选确认时，多目的地也必须和单目的地一样走状态机逐步确认（目的地→交通→酒店→
    餐饮→行程），逐段查交通、逐城查酒店；多城能力由 step_config 各步骤的"多目的地分支"
    提供，不再由本通道接管（此前接管会导致用户失去逐步确认的选择权）。
    """
    # 1. 词表快筛：未提及自动确认 → 直接回落状态机（零 LLM 开销）
    # ★ 第一层：0ms 词表快筛，普通消息在这里就被挡掉，一次 LLM 都不花
    if not _hit_auto_confirm(content):
        return None

    # 2. 结构化提取复核（~3.7s，防止用户只在闲聊里提到"自动确认"误入）
    # 目的地允许为空：auto_confirm 时由快速通道自动选定（见 run_express_planning）
    # ★ 第二层：命中正则才花 ~3.7s 做 LLM 复核，防"闲聊里顺嘴提到自动确认"误入
    req = await extract_requirement(content)
    if not req.departure_city or not req.auto_confirm:
        app_logger.info("[express] 提取复核未通过，回落状态机")
        return None

    app_logger.info(
        f"[express] 命中快速通道: {req.destination or '(无目的地，AI 自动选定)'} "
        f"{req.departure_date} {req.travel_days}天"
    )
    return req

class DestinationPick(BaseModel):
    """自动选目的地的结构化返回。

    早期实现让模型"只输出一行，格式严格为：城市名|推荐理由"，代码再 split("|") 取两段。
    那是一条**只写在提示词里的私有协议**：模型多写一句开场白、把竖线写成全角、
    或者干脆换行输出，代码就只能拿到半截——而且拿到半截不会报错，只会默默回落状态机，
    用户看到的是"没给我选目的地"。改用 schema 之后，字段名和类型由 Pydantic 说了算
    """

    city: str = Field(description='目的地城市名，如"杭州"；只写城市，不带省份、不带"市"字')
    reason: str = Field(description="推荐理由，40 字以内，说明为什么匹配这次出行")

_PICK_DEST_PROMPT = (
    "你是目的地推荐专家。用户信息：从 {departure_city} 出发，{departure_date} 出行 "
    "{travel_days} 天，{people} 人，人均预算≤{budget}元，交通方式：{transport}，"
    "旅行风格：{styles}。\n"
    "从国内真实城市中选出最匹配的一个目的地。要求：\n"
    "1. 从出发地乘所选交通方式可达（高铁优先 5 小时内）\n"
    "2. 匹配{season}的季节特征与旅行风格（如 culture→历史人文名城，food→美食之都）\n"
    "3. 按 schema 填写 city 与 reason 两个字段，不要输出多余文字"
)

async def _pick_destination(req: ExpressRequirement) -> DestinationPick | None:
    """无目的地时 AI 一次调用直接选定最优城市（不过 RAG/multi-agent，~3s）。
    返回结构化的 city / reason；模型没给出合规结构时返回 None（调用方回落状态机）"""
    season = {"3": "春季", "4": "春季", "5": "春季", "6": "夏季", "7": "夏季", "8": "夏季",
              "9": "秋季", "10": "秋季", "11": "秋季", "12": "冬季", "1": "冬季", "2": "冬季"}.get(
        req.departure_date[5:7], "")
    llm = ChatTongyi(
        model=settings.qwen_model_name,
        api_key=settings.dashscope_api_key,
        temperature=0.3,
    )
    prompt = _PICK_DEST_PROMPT.format(
        departure_city=req.departure_city, departure_date=req.departure_date,
        travel_days=req.travel_days,
        people=f"{req.adult_count}成人{req.children_count}儿童",
        budget=req.budget_max or "不限", transport="/".join(req.transport_types) or "高铁",
        styles="、".join(req.travel_styles) or "不限", season=season,
    )
    # ★ 与本文件的 extract_requirement 同一套写法：with_structured_output 让模型按字段填
    structured = llm.with_structured_output(DestinationPick)
    try:
        picked: DestinationPick = await structured.ainvoke(prompt)
    except Exception:
        app_logger.warning("[express] 目的地自动选定失败：模型未给出合规结构", exc_info=True)
        return None
    city = (picked.city or "").strip()
    # ★ 长度守卫保留：schema 管得了"有没有这个字段"，管不了"填得合不合理"——
    #   模型偶尔会把"杭州（西湖、灵隐寺）"这类带括注的串塞进 city
    if not city or len(city) > 10:
        app_logger.warning(f"[express] 目的地自动选定不合规: city={picked.city!r}")
        return None
    picked.city = city
    app_logger.info(f"[express] 自动选定目的地: {city}（{picked.reason}）")
    return picked

# ============== 步骤 2：并行数据收集 ==============

def _iata(city: str) -> str:
    """城市名 → 机场三字码（searchFlightsByDepArr 的 depcity/arrcity 必需）；
    查不到返回空串。码表统一取自 mcp_tools._CITY_AIRPORT（与票价用的城市码分开维护）"""
    from app.tools.mcp_tools import _CITY_AIRPORT
    name = (city or "").strip()
    if not name:
        return ""
    for suffix in ("市", "地区", "自治州"):
        if name.endswith(suffix) and len(name) > len(suffix):
            name = name[: -len(suffix)]
            break
    return _CITY_AIRPORT.get(name, "")


# ============== 多目的地路段构造 ==============

_CITY_DELIMS = "，,、"


def _split_cities(destination: str) -> list[str]:
    """把 destination 按中/英逗号、顿号切分为城市列表（去空去重保序）"""
    if not destination:
        return []
    flat = destination
    for ch in _CITY_DELIMS:
        flat = flat.replace(ch, " ")
    out: list[str] = []
    for city in flat.split():
        if city not in out:
            out.append(city)
    return out


def ordered_play_cities(departure: str, destination: str) -> list[str]:
    """游玩/住宿节点城市序列（去重保序）：出发地仅当本身也在目的地串里才计入，且置于最前。
    住宿站点 = 目的地城市序列。
    示例：北京→上海 = [上海]；北京→天津/上海 = [天津,上海]；北京→北京 = [北京]；
    目的地含出发地（北京→北京/天津）时：[北京,天津]
    """
    cities = _split_cities(destination)
    dep = (departure or "").strip()
    ordered = (([dep] if dep and dep in cities else []) + cities)
    out: list[str] = []
    for c in ordered:
        if c and c not in out:
            out.append(c)
    return out


def _plan_cities(req: ExpressRequirement) -> list[str]:
    """规划用游玩城市序列（快速通道）——口径与住宿站点完全同源"""
    return ordered_play_cities(req.departure_city, req.destination)


def _build_legs(departure: str, cities: list[str]) -> list[tuple[str, str]]:
    """构造交通路段序列：出发→首城、城际转场、末城→出发地返程。
    剔除自身段：出发地与目的地相同，跳过"北京→北京"无效路段，保留本地游玩。
    route = [出发地] + [目的地列表] + [出发地(返程)]；仅相邻城市不同才生成路段。
    示例：北京→上海 = 2段；北京→天津→上海 =3段；北京→北京 =0段；出发地缺失不补返程。
    """
    if not cities:
        return []
    route = [departure] + cities + ([departure] if departure else [])
    return [(a, b) for a, b in zip(route[:-1], route[1:]) if a != b]


def _stay_nights(days: int, stations: int) -> list[int]:
    """住宿晚数分配：出发地固定1晚，剩余天数在其后城市均分，前面城市多分配余数天数。
    酒店段覆盖全部节点城市（含出发地）。天数不足时每站至少1晚，不自动删除城市。
    """
    if stations <= 0 or days <= 0:
        return []
    if stations == 1:
        return [days]
    rest = max(1, days - 1)
    per, rem = divmod(rest, stations - 1)
    return [1] + [per + (1 if i < rem else 0) for i in range(stations - 1)]


async def _collect_context(req: ExpressRequirement, stream) -> dict:
    """并行收集快速通道所需数据：按段真查交通 + 每城酒店/天气，单项失败降级不阻塞

 多目的地时对每段交通（去程/城际转场/返程）独立真查，
    每个目的地城市独立查酒店与天气——各段/各城之间互不影响，任一失败独立降级。
    """
    from app.tools.mcp_tools import (
        _CITY_IATA, flight_price_args, get_flight_tools, get_train_tools,
        get_hotel_tools, get_weather_tools,
    )

    stream.push(make_event(ROLE_ASSISTANT, "⚡ 已锁定需求，正在并行查询交通、酒店与天气…\n"))

    flight_tools = await get_flight_tools()
    train_tools = await get_train_tools()
    hotel_tools = await get_hotel_tools()
    weather_tools = await get_weather_tools()

    # 多目的地路段与城市列表（单目的地时 cities=[dest]、legs=[出发→dest]）
    # 用 _plan_cities——目的地含出发地时，出发地也是游玩地（北京本地+去天津）
    cities = _plan_cities(req)
    legs = _build_legs(req.departure_city, cities)
    app_logger.info(f"[express] 路段规划: cities={cities} legs={legs}")

    async def _safe(name: str, tool, kwargs: dict) -> str:
        """单项查询兜底：失败返回空串并记日志，绝不让单点故障阻塞快速通道"""
        if tool is None:
            # ★ 工具定位失败必须留痕：此前这里是静默 return ""，"航班查不到"在日志里
            #   完全看不出来（无降级警告、无跳过提示，只见 flight_0 长度为 0）
            app_logger.warning(f"[express] {name} 未定位到工具，本次查询跳过（检查 _first 关键词）")
            return ""
        try:
            raw = await tool.ainvoke(kwargs)
            # ainvoke 返回 content blocks 列表 [{'type':'text','text':...}]，提取纯文本
            if isinstance(raw, list):
                return "\n".join(
                    b.get("text", "") for b in raw if isinstance(b, dict)
                )
            return str(raw)
        except Exception as e:
            app_logger.warning(f"[express] {name} 查询失败降级: {e}")
            return ""

    def _first(tools, keyword: str):
        """按关键词取第一个匹配工具（大小写不敏感；找不到返回 None，由 _safe 兜底）

        ⚠️ 两边都要 lower()：工具名是 MCP 给的驼峰（searchFlightsByDepArr），
        此前只 lower 了工具名、关键词照抄驼峰，于是航班工具**永远定位不到** →
        _safe 直接返回空串 → flight_0/flight_1 恒为 0 字，摘要永远"未查询到实时班次"；
        而关键词本来全小写的 get-tickets/searchhotels/get_weather_forecast 三个都正常，
        所以故障只出现在快速通道的航班分支（手动通道是模型自己调工具、不走这里）。
        """
        key = keyword.lower()
        for t in tools:
            if key in t.name.lower():
                return t
        return None

    jobs = {}
    # 段日期与状态机同源（_leg_query_date）：去程=出发日、城际=下一城首日、返程=末日
    from app.core.middleware import _leg_query_date
    _leg_state = {
        "user_requirement": {"departure_city": req.departure_city,
                             "departure_date": req.departure_date,
                             "travel_days": req.travel_days},
        "selected_destination": req.destination,
    }
    _leg_names = [f"{a}→{b}" for a, b in legs]
    leg_dates = [_leg_query_date(_leg_state, _leg_names, i) or req.departure_date
                 for i in range(len(legs))]
    # 交通按段（去程/转场/返程）逐段查询，key 形如 "train_0"、"train_1"
    if "train" in req.transport_types:
        t = _first(train_tools, "get-tickets")
        for i, (src, dst) in enumerate(legs):
            jobs[f"train_{i}"] = _safe(f"高铁{i}", t, {
                "date": leg_dates[i], "fromStation": src, "toStation": dst,
            })
    if "flight" in req.transport_types:
        t = _first(flight_tools, "searchFlightsByDepArr")
        # ★ 时刻表（searchFlightsByDepArr）是**航班动态**接口，多数航线不含票价，
        #   故另配票价工具，两段一起并发查（与 flight_multi_segment._query_one_leg 同口径）
        pt = _first(flight_tools, "getFlightPriceByCities")
        for i, (src, dst) in enumerate(legs):
            dep, arr = _iata(src), _iata(dst)   # 机场码：时刻表 depcity/arrcity
            if not (dep and arr):
                # ★ 片③ 的注释一直写着"未收录城市跳过航班查询"，但代码此前照样把空串传进去：
                #   工具必定返回空，用户看到的是"未查询到实时班次"，与"该航线今天真没航班"
                #   分不清，日志里还不留痕。现在真的跳过，并把缺码的城市打出来
                missing = "、".join(c for c, code in ((src, dep), (dst, arr)) if not code)
                app_logger.warning(
                    f"[express] 航班段 {src}→{dst} 跳过：{missing} 不在 _CITY_AIRPORT 表里"
                    f"（searchFlightsByDepArr 要机场三字码，不收城市名）"
                )
                continue
            jobs[f"flight_{i}"] = _safe(f"航班{i}", t, {
                "date": leg_dates[i], "depcity": dep, "arrcity": arr,
            })
            if pt is not None:      # 票价查不到只丢价格、不丢航班（_safe 已兜底）
                # ★ 票价接口要**城市**码（北京 BJS），与上面的**机场**码（北京 PEK）不是一套表，别混用
                cdep, carr = _CITY_IATA.get(src), _CITY_IATA.get(dst)
                if cdep and carr:
                    jobs[f"flightprice_{i}"] = _safe(
                        f"航班价{i}", pt, flight_price_args(pt, leg_dates[i], cdep, carr))
                else:
                    app_logger.info(
                        f"[express] 航班价 {src}→{dst} 跳过：城市码缺失（{cdep}/{carr}）"
                    )
    # 酒店/天气按每个目的地城市独立查，key 形如 "hotel_0"、"weather_1"
    # 酒店逐城窗口：首站固定 1 晚、其余天数在其后城市均分（_stay_nights），checkIn 随城市逐城
    # 累加，末站离店日封顶到末日（总晚数 N-1）——与 hotel_flow._stay_windows 同源。
    # 此前每城都传"出发日 + N-1 晚"，与各城真实住宿窗口不符（多城目的地会重复计晚）。
    nights_by_city = _stay_nights(req.travel_days, len(cities))
    try:
        start = datetime.fromisoformat(req.departure_date[:10])
    except ValueError:
        start = None
        app_logger.warning(f"[express] 出发日期不可解析，酒店按出发日查询: {req.departure_date!r}")
    offset = 0
    last_out = max(req.travel_days - 1, 1)
    for i, city in enumerate(cities):
        night = nights_by_city[i] if i < len(nights_by_city) else 0
        cin, cout = offset, min(offset + night, last_out)
        offset += night
        if cout > cin:          # 末站当天到、当天返程：该城无需住宿，跳过酒店查询（天气照查）
            jobs[f"hotel_{i}"] = _safe(f"酒店{i}", _first(hotel_tools, "searchhotels") or _first(hotel_tools, "find-hotels"), {
                "place": city,
                "checkIn": (start + timedelta(days=cin)).strftime("%Y-%m-%d") if start else req.departure_date,
                "stayNights": cout - cin, "adultCount": req.adult_count, "size": 5,
            })
        jobs[f"weather_{i}"] = _safe(f"天气{i}", _first(weather_tools, "get_weather_forecast"), {
            # 实测：date 仅支持未来 4 天内，超出返回空——不传日期拿全部预报，选定器再截取
            "city": city,
        })

    # ★ jobs 用 dict：key 自带"哪段/哪城"，gather 回来 zip 一下就能对上号
    keys = list(jobs)
    # ★ 所有查询同时发出、只等最慢那一个；_safe 已吃掉单点异常，gather 永远不会整体失败
    results = await asyncio.gather(*jobs.values())
    ctx = dict(zip(keys, results))
    app_logger.info(f"[express] 并行收集完成: {[(k, len(v)) for k, v in ctx.items()]}")
    # 把路段/城市序列一并返回，供选定器与生成器复用
    ctx["_cities"] = cities
    ctx["_legs"] = legs
    return ctx

# ============== 步骤 3：规则选定器（纯代码 0s，替代状态机的 LLM 判断） ==============
_SEAT_ORDER = ("二等座", "硬座", "一等座", "硬卧", "软卧", "无座")
# 有票状态词：12306 原始返回是"有票/剩余N张票"，守卫压缩后的紧凑行是"有/剩余N张票"，两种都要认
_AVAIL_STATUS = ("有票", "有", "候补")

def _dur_to_mins(dur: str) -> int:
    """历时 HH:MM → 分钟；解析不出给一个极大值（排最后，不会被选中）"""
    try:
        h, m = str(dur).split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return 10 ** 9

def _seat_available(status: str) -> bool:
    """余票状态是否可购（有票/剩余N张/候补都算，无票/-- 不算）"""
    s = (status or "").strip()
    return s in _AVAIL_STATUS or s.startswith("剩余") or s.endswith("张")

def _pick_train(text: str) -> dict | None:
    """从 12306 文本选最优班次：原始格式与守卫压缩后的紧凑格式都支持。
    在可购票班次中，按「历时分钟 + 价格/10」综合最低筛选；耗时优先、兼顾价格，纯代码无随机。
    复用两套解析器：原始多行格式用 parse_ticket_rows，压缩单行格式用 _compact_to_rows，保证两种输入都可解析。
    """
    # 就近 import 避免顶层循环依赖（本模块其它函数也按需从 mcp_tools 导入）
    from app.tools.mcp_tools import parse_ticket_rows
    from app.capabilities.train_multi_segment.train_agent_tool import _compact_to_rows, train_score

    rows = parse_ticket_rows(text) or _compact_to_rows(text)
    best = None
    for r in rows:
        seats = r.get("seats") or {}
        # 席别优先序取第一个有数据的（与 _SEAT_ORDER 同口径，通常是二等座）
        seat = next((k for k in _SEAT_ORDER if k in seats), None)
        if not seat:
            continue
        status, price = seats[seat]
        if not _seat_available(status):  # 只在有票里挑，无票整趟跳过
            continue
        mins = _dur_to_mins(r.get("dur"))
        score = train_score(mins, float(price))  # 与状态机自动代选同口径（单一公式，避免漂移）
        if best is None or score < best["score"]:
            best = {"no": r["code"], "dep": r["dep"], "arr": r["arr"],
                    "mins": mins, "price": float(price), "score": score}
    return best

def _pick_flight(text: str, price_text: str = "") -> dict | None:
    """从航班查询结果选最优航班；解析失败返回 None，交由上层降级兜底
    数据源为 searchFlightsByDepArr 航班动态接口，多数航线不带票价，分两步处理：
    ① parse_flight_rows 解析得到航班基础信息；
    ② parse_flight_prices 按航班号补充票价。
    有价则取最低价；无票价则取最早起飞航班，价格置0，摘要标记价格待定，不再丢弃无价格航班。
    """
    from app.tools.mcp_tools import parse_flight_prices, parse_flight_rows

    rows = [
        {"flight_no": r["code"], "price": float(r.get("price") or 0),
         "dep": r.get("dep") or "", "arr": r.get("arr") or ""}
        for r in parse_flight_rows(text) if r.get("code")
    ]
    if not rows:
        return None

    by_flight, _lowest = parse_flight_prices(price_text) if price_text else ({}, 0.0)
    for r in rows:
        if not r["price"]:
            r["price"] = float(by_flight.get(r["flight_no"].upper()) or 0)

    priced = [r for r in rows if r["price"] > 0]
    if priced:
        return min(priced, key=lambda r: r["price"])
    return min(rows, key=lambda r: r.get("dep") or "")

def _pick_hotel(text: str) -> dict | None:
    """从酒店 JSON 选最优：与状态机自动代选同源（hotel_agent_tool.hotel_rank）——
    剔除异常低价（<25 元/晚）后：评分≥4.5 取最低价，否则取评分最高（同分取便宜）"""
    # 就近 import 避免顶层循环依赖（本模块其它择优函数也按需从 capabilities 导入）
    from app.capabilities.hotel_multi_segment.hotel_agent_tool import _MIN_HOTEL_PRICE, hotel_rank
    try:
        data = json.loads(text)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None

    hotels = []
    for h in data.get("hotelInformationList") or []:
        if not isinstance(h, dict) or not h.get("name"):
            continue
        price_info = h.get("price")
        message = price_info.get("message", "") if isinstance(price_info, dict) else ""
        price = _number_before(message, "CNY/晚")
        if price:
            hotels.append({"name": h["name"], "price": price,
                           "score": _to_float(h.get("starRating")), "address": h.get("address", "")})

    valid = [h for h in hotels if h["price"] >= _MIN_HOTEL_PRICE]  # 接口偶发返回"14元/晚"这类失真价格
    if not valid:
        return None
    return min(valid, key=lambda h: hotel_rank(h["price"], h["score"]))

def _pick_weather(text: str, date: str) -> str:
    """从高德预报 JSON 截取出发日期当天摘要，取不到就退第一条"""
    try:
        casts = json.loads(text).get("casts", [])
        c = next((x for x in casts if x.get("date") == date), casts[0] if casts else None)
        return f"{c['dayweather']} {c.get('daytemp')}~{c.get('nighttemp')}°C" if c else ""
    except Exception:
        return ""

def _fallback_hotel_brands(hotel_preferences: list[str] | None) -> str:
    """酒店报价不可用时的品牌兜底文案，延迟导入防止模块循环依赖"""
    try:
        # 这里的报错是因为这个没有创建
        # _COMMON_HOTELS的数据是
        """
            在环节16.1
            _COMMON_HOTELS: dict[str, list[str]] = {
                "economy_hotel": ["如家", "汉庭", "7 天", "锦江之星", "格林豪泰", "莫泰"],
                "star_hotel": ["希尔顿", "万豪", "洲际", "香格里拉", "凯悦", "雅高"],
                "hostel": ["花间堂", "途家民宿", "小猪民宿", "美团民宿"],
                "youth_hostel": ["国际青年旅舍（YHA）", "万里路青年旅舍", "如家青旅"],
            }
        """
        from app.services.chat_stream_executor import _COMMON_HOTELS
    except Exception:
        _COMMON_HOTELS = {"economy_hotel": ["如家", "汉庭", "7 天"]}
    types = [t for t in (hotel_preferences or []) if t in _COMMON_HOTELS] or ["economy_hotel"]
    return "、".join(_COMMON_HOTELS[types[0]][:3])


def _select_choices(req: ExpressRequirement, ctx: dict) -> dict:
    """规则选定：逐段交通、逐城酒店、餐饮一次性选定，返回结果与用户摘要文本"""
    cities = ctx.get("_cities") or _split_cities(req.destination)
    legs = ctx.get("_legs") or _build_legs(req.departure_city, cities)
    food = req.food_preferences or (["specialty", "local"] if "food" in req.travel_styles else ["local", "specialty"])

    transports = []          # 成功获取的交通，用于预算计算
    leg_results = []         # 全部路段记录，包含查询失败路段
    for i, (src, dst) in enumerate(legs):
        # ★ 按用户勾选顺序挑交通（req.transport_types 即偏好序），不硬编码 train 优先：
        #   逐个模式试，取首个有有效结果的；查询侧只对该模式建了 job，无候选自然跳过
        picked = None
        for mode in req.transport_types:
            if mode == "train":
                t = _pick_train(ctx.get(f"train_{i}", ""))
                if t:
                    picked = {"type": "train", **t}
            elif mode == "flight":
                f = _pick_flight(ctx.get(f"flight_{i}", ""), ctx.get(f"flightprice_{i}", ""))
                if f:
                    picked = {"type": "flight", **f}
            if picked:
                break
        if picked:
            picks = dict(picked)
            picks["src"], picks["dst"] = src, dst
            transports.append(picks)
            leg_results.append(("ok", src, dst, picks))
        else:
            leg_results.append(("miss", src, dst, None))

    hotels = []
    hotel_results = []       # 全部城市酒店记录，包含兜底 fallback
    for i, city in enumerate(cities):
        h = _pick_hotel(ctx.get(f"hotel_{i}", ""))
        if h:
            buildings = dict(h)
            buildings["city"] = city
            hotels.append(buildings)
            hotel_results.append(("ok", city, buildings))
        else:
            hotel_results.append(("fallback", city, None))

    weathers = {}
    for i, city in enumerate(cities):
        w = _pick_weather(ctx.get(f"weather_{i}", ""), req.departure_date)
        if w:
            weathers[city] = w

    sel = {
        "transports": transports,
        "hotels": hotels,
        "food": food,
        "weathers": weathers,
        "cities": cities,
        "total_legs": len(legs),
        "leg_results": leg_results,
        "hotel_results": hotel_results
    }

    lines = []
    # 交通摘要，按路段序号输出，失败路段保留提示
    for i, (kind, src, dst, pk) in enumerate(leg_results):
        if kind == "ok" and pk["type"] == "train":
            lines.append(f"🚄 交通{i+1}（{src}→{dst}）：{pk['no']} 次 {pk['dep']}出发（历时{pk['mins']//60}小时{pk['mins']%60:02d}分，二等座{pk['price']:.0f}元）")
        elif kind == "ok":
            price_txt = f"{pk['price']:.0f}元" if pk["price"] else "价格待定"
            dep_txt = f"{pk['dep']}出发，" if pk.get("dep") else ""
            lines.append(f"✈️ 交通{i+1}（{src}→{dst}）：{pk.get('flight_no','航班')} {dep_txt}{price_txt}")
        else:
            lines.append(f"🚄 交通{i+1}（{src}→{dst}）：未查询到实时班次（可到站现买或改期，费用按估算计入）")

    # 酒店摘要，无真实报价使用品牌兜底文案
    for kind, city, h in hotel_results:
        if kind == "ok":
            lines.append(f"🏨 {city}酒店：{h['name']}（{h['price']:.0f}元/晚，评分{h['score']}）")
        else:
            lines.append(f"🏨 {city}住宿：经济连锁（{_fallback_hotel_brands(req.accommodation_types)}等，到店择优选同档位）")

    lines.append("🍽️ 餐饮偏好：" + "、".join({"specialty": "特色美食", "chain": "连锁快餐", "local": "本地小吃"}[f] for f in food))
    for city, w in weathers.items():
        lines.append(f"🌤️ {city}出发日天气：{w}")

    sel["summary"] = "\n".join(lines) if lines else "未查询到实时数据，正在按常识安排…"
    app_logger.info(f"[express] 规则选定完成: {sel['summary'][:100]}...")
    return sel

# ============== 步骤 4：单次 LLM 结构化行程 + 预算模板拼接 ==============

_ITIN_PROMPT = (
    "你是行程规划专家。根据已锁定的信息生成逐日行程，这是全流程唯一的长输出。\n"
    "已锁定信息：\n{info}\n\n"
    "硬规则：\n"
    "1. 全程正好 {days} 天，day 字段从 1 连续编号到 {days}，不重不漏\n"
    "2. 每天四个时段 morning / noon / afternoon / evening 都要有内容，每段 1~2 条，"
    "每条不少于 15 字：时间/地点 + 怎么玩或看点 + 价格或耗时\n"
    "3. 每天至少 1 条 tips（预约/门票/交通接驳/避坑）\n"
    "4. 最后一天的 evening 必须包含返程安排\n"
    "5. 餐饮遵循偏好：{food}；多类型每类都体现，特色美食给具体店名\n"
    "6. [多目的地] 若 {cities} 有多个城市，city 字段写当天所在城市，"
    "按已锁定的交通路段在对应日期标明转场；不得漏掉任一城市\n"
    "7. 严禁散文描写、历史科普、客套话\n"
    "8. **只按 schema 提交结构化数据**：正文由系统按你提交的结构渲染，"
    "不要自己写 Markdown 正文、不要写预算表、不要写开场白"
)


def _day_cities(req: ExpressRequirement) -> list[str]:
    """逐日城市序列（第 1..N 天 → 城市）：与住宿站点、段日期**同源**"""
    nodes = _plan_cities(req)
    if not nodes or req.travel_days <= 0:
        return []
    out: list[str] = []
    for node, night in zip(nodes, _stay_nights(req.travel_days, len(nodes))):
        out.extend([node] * night)
    return out

from pydantic import ValidationError
async def _generate_itinerary(req: ExpressRequirement, sel: dict, stream) -> list[dict] | None:
    """唯一一次 LLM 长输出：让模型**提交结构化逐日行程**，正文交给代码渲染

    返回已过 ItineraryBody 校验的逐日结构（list[dict]）；拿不到合规结构返回 None，
    调用方回落状态机
    """
    # 游玩城市以"计划城市"为准（含出发地就地游玩），
    # 并集上已选到酒店的城，保证酒店某城缺失也不丢该城游玩日
    plan_cities = _plan_cities(req)
    cities = _plan_cities(req) if not sel["hotels"] else \
        list(dict.fromkeys([h["city"] for h in sel["hotels"]] + plan_cities))
    # 交通按段、酒店按城、天气按城拼进 info（单目的地退化为一段/一城，行为不变）
    # ★ 用 leg_results（含失败段）拼**完整路线**：此前只拼成功段，缺了中间段（如上海→天津）
    #   会让模型自行脑补转场、把返程车次张冠李戴到错误路段。完整路线交给模型才不串场。
    leg_results = sel.get("leg_results")
    if leg_results:
        parts = []
        for kind, src, dst, pk in leg_results:
            if kind == "ok":
                code = pk["no"] if pk["type"] == "train" else pk.get("flight_no", "")
                parts.append(f"{src}→{dst}{code}")
            else:
                parts.append(f"{src}→{dst}(实时班次未查到，按常识安排该段)")
        transport_line = "；".join(parts)
    elif sel["transports"]:
        transport_line = "；".join(
            f"{tr['src']}→{tr['dst']}{tr['no'] if tr['type']=='train' else tr.get('flight_no','')}"
            for tr in sel["transports"])
    else:
        transport_line = "未查询到实时班次，按常识安排往返交通"
    if sel["hotels"]:
        hotel_line = "；".join(
            f"{h['city']}:{h['name']}({h['price']:.0f}元/晚)" for h in sel["hotels"])
    else:
        hotel_line = "未查询到实时酒店，按经济型预算合理安排（到店再定）"
    weather_line = "；".join(f"{c}:{w}" for c, w in sel["weathers"].items()) or "未知"
    info = (
        f"- 目的地：{req.destination}｜出发：{req.departure_date}｜{req.travel_days}天｜"
        f"{req.adult_count}成人{req.children_count}儿童｜预算人均≤{req.budget_max or '不限'}元\n"
        f"- 交通：{transport_line}\n"
        f"- 酒店：{hotel_line}\n"
        f"- 天气：{weather_line}｜风格：{req.travel_styles}"
    )
    city_seq = "、".join(cities) if len(cities) > 1 else "无（单一目的地）"
    prompt = _ITIN_PROMPT.format(
        info=info, days=req.travel_days, cities=city_seq,
        food="、".join({"specialty": "特色美食", "chain": "连锁快餐", "local": "本地小吃"}[f]
                       for f in sel["food"]))
    llm = ChatTongyi(
        model=settings.qwen_model_name,
        api_key=settings.dashscope_api_key,
        temperature=settings.qwen_temperature,
    )
    stream.push(make_event(ROLE_ASSISTANT, "\n📝 正在生成逐日行程…\n"))
    # ★ 全项目"逐日行程"的结构只有一份定义：复用第 11 章的 ItineraryBody 与代码归一助手，
    #   快速通道与状态机通道不会各写一套 schema
    from app.tools.state_transition import ItineraryBody, _normalize_itinerary_days

    #   OpenAI 兼容客户端（ChatOpenAI）
    bound = llm.bind_tools(
        [ItineraryBody],
        tool_choice={"type": "function", "function": {"name": "ItineraryBody"}},
    )
    try:
        res = await bound.ainvoke(prompt)
        raw_days = list((res.tool_calls[0]["args"] if res.tool_calls else {}).get("days") or [])
    except Exception:
        app_logger.warning("[express] 逐日行程结构化调用失败", exc_info=True)
        return None
    if not raw_days:
        app_logger.warning("[express] 模型未返回逐日行程结构（无 tool_calls）")
        return None
    # ★ 第二级：天数/时段/返程这些"必须成立"的规则由代码归一，不让模型重写整份
    back = f"傍晚按已确认的交通班次返程回到{req.departure_city}"
    fixed, fixes = _normalize_itinerary_days(
        raw_days, req.travel_days, back_text=back, cities=_day_cities(req))
    try:
        body = ItineraryBody.model_validate(
            {"days": fixed}, context={"travel_days": req.travel_days})
    except ValidationError as exc:
        # ★ 第三级：归一后仍不合规 → 确定性降级，回落状态机，绝不回头去解析正文
        app_logger.warning(f"[express] 逐日行程归一后仍未通过校验：{exc.errors()[:2]}")
        return None
    if fixes:
        app_logger.warning(
            f"[express] 已按 {req.travel_days} 天规范化 {len(fixes)} 处：{'；'.join(fixes[:6])}")
    days_out = [d.model_dump() for d in body.days]
    app_logger.info(f"[express] 逐日行程结构就绪：{len(days_out)} 天（末日含返程）")
    return days_out

def _build_budget(req: ExpressRequirement, sel: dict) -> tuple[str, dict]:
    """纯代码预算计算与明细拼接，口径与 summarize_budget_tool 保持一致，不经过LLM复述。
    返回 (展示文本, 结构化明细)——后者与 state.BudgetBreakdown 同构，供同步进状态机 budget 字段"""
    import math
    people = req.adult_count + req.children_count
    days, nights = req.travel_days, max(req.travel_days - 1, 1)
    rooms = max(math.ceil(people / 2), 1)

    cities = sel.get("cities") or _split_cities(req.destination)
    total_legs = sel.get("total_legs") or len(_build_legs(req.departure_city, cities))

    # 交通费用：真实票价求和，缺失路段按已获取路段均价估算
    leg_prices = [tr["price"] for tr in sel["transports"] if tr["price"] > 0]
    if leg_prices:
        avg_leg = sum(leg_prices) / len(leg_prices)
        transport_price = sum(leg_prices) + avg_leg * max(total_legs - len(leg_prices), 0)
    else:
        transport_price = 500 * max(total_legs, 1)
    transport_cost = transport_price * people

    # 住宿费用：取各城酒店均价，无数据时使用300元/晚估算
    city_prices = [h["price"] for h in sel["hotels"]]
    hotel_price = (sum(city_prices) / len(city_prices)) if city_prices else 300
    accommodation_cost = hotel_price * nights * rooms

    food_cost = 150 * days * people
    attractions_cost = 200 * days * people
    misc_cost = 100 * days * people

    total = transport_cost + accommodation_cost + food_cost + attractions_cost + misc_cost
    per_person = total / people if people else total

    if req.budget_max > 0:
        verdict = "人均未超预算" if per_person <= req.budget_max else "人均超预算（建议下调住宿档位或减天数）"
    else:
        verdict = "未提供预算上限，仅作参考"

    missing_legs = max(total_legs - len(leg_prices), 0)
    if not leg_prices:
        transport_detail = f"（{total_legs} 段按常识估算）"
    elif missing_legs:
        transport_detail = f"（{len(leg_prices)}/{total_legs} 段实时票价，{missing_legs} 段按均价估算）"
    else:
        transport_detail = f"（{total_legs} 段 × 各段票价）"

    hotel_detail = (f"各城均价 {hotel_price:.0f} 元"
                    if city_prices else f"各城估算 {hotel_price:.0f} 元/晚（实时房价不可用）")

    text = (
        f"### 预算明细（{people} 人 / {nights} 晚 / {rooms} 间房 / {len(cities) or 1} 城）\n"
        f"- **交通费用**：{transport_cost:.2f} 元（人均 {transport_price:.0f} 元，{transport_detail}）\n"
        f"- **住宿费用**：{accommodation_cost:.2f} 元（{rooms} 间 × {nights} 晚 × {hotel_detail}）\n"
        f"- **餐饮费用**：{food_cost:.2f} 元（150元/人/天）\n"
        f"- **景点门票**：{attractions_cost:.2f} 元（按 200元/人/天 估算）\n"
        f"- **其他杂费**：{misc_cost:.2f} 元\n"
        f"**总计：{total:.2f} 元（{people} 人总价）｜人均：{per_person:.2f} 元**\n"
        f"预算结论：{verdict}"
    )
    # ★ 结构化明细（字段与 state.BudgetBreakdown 同构），供 sync_express_state 写入状态机
    breakdown = {
        "transport": transport_cost,
        "accommodation": accommodation_cost,
        "food": food_cost,
        "attractions": attractions_cost,
        "misc": misc_cost,
        "total": total,
    }
    return text, breakdown

async def run_express_planning(req: ExpressRequirement, stream):
    """快速通道主流程：收集 → 选定 → 生成 → 预算拼接，返回 (完整回复文本, 逐日结构, 同步上下文)。
    同步上下文含 sel/breakdown/itinerary_body，供调用方落库后一次性写入状态机；
    调用方（chat_contract_service）拿到返回值后走既有清单生成/压缩/落库链路；
    状态机记忆同步不在此处处理，挪至清单落库后与行程合并执行一次 aupdate_state，
    分两次写入会触发 Ambiguous update 异常。
    无目的地场景：auto_confirm模式下AI自动选定最优目的地（不经过RAG），
    选定失败返回 None，调用方回落状态机。
    """
    if not req.destination:
        picked = await _pick_destination(req)
        if not picked:
            app_logger.warning("[express] 目的地自动选定失败，回落状态机")
            return None, None, None
        req.destination, reason = picked.city, picked.reason
        stream.push(make_event(
            ROLE_ASSISTANT, f"🏛️ 已为您选定目的地：{req.destination}（{reason}）\n\n"))

    stream.push(make_event(ROLE_ASSISTANT, "🚀 已识别自动确认需求，启用极速规划模式\n"))

    # 多目的地天数调整：按游玩城市数×2规划紧凑行程，天数不足自动上调并告知用户，单目的地不受影响
    # 使用 _plan_cities 计算，目的地包含出发地时，出发地计入游玩城市
    _cities = _plan_cities(req)
    if len(_cities) > 1:
        _needed = len(_cities) * 2
        if req.travel_days < _needed:
            _old_days = req.travel_days
            req.travel_days = _needed
            stream.push(make_event(ROLE_ASSISTANT,
                f"📅 由于您选择的是 {_old_days} 天，多目的地行程时间比较紧凑，已为您调整到 {_needed} 天\n\n"))

    ctx = await _collect_context(req, stream)
    sel = _select_choices(req, ctx)
    stream.push(make_event(ROLE_ASSISTANT, f"\n{sel['summary']}\n\n"))

    days = await _generate_itinerary(req, sel, stream)
    if not days:
        # 拿不到逐日结构绝不静默出预算（实测：无行程仍推预算，用户只见"正在生成逐日行程…"后跳预算）
        # 回落状态机：调用方已有 None 分支（chat_contract_service），状态机行程步同样强推落库工具
        app_logger.warning("[express] 逐日行程生成失败，放弃快速通道，回落状态机")
        return None, None, None
    budget, breakdown = _build_budget(req, sel)

    # 将结构化逐日行程渲染为Markdown正文推送给前端
    from app.services.chat_stream_executor import _push_rendered, _render_itinerary_body
    body = _render_itinerary_body(
        days or [],
        {},
        {
            "adult_count": req.adult_count,
            "children_count": req.children_count,
            "budget_max": req.budget_max,
        },
    )
    if body:
        await _push_rendered(stream, f"\n\n{body}")

    stream.push(make_event(ROLE_ASSISTANT, "\n\n" + budget))
    #   同时返回逐日结构：清单提炼失败时调用方要靠它兜底（此前只返回文本，
    #   DeepSeek 欠费/不返工具调用时直接抛"无可用的结构化行程可兜底"）
    sync = {"sel": sel, "breakdown": breakdown, "itinerary_body": days}
    return f"{sel['summary']}\n\n{body}\n\n{budget}", days, sync

# ============== 步骤 5.5：短期记忆同步 ==============
async def sync_express_state(conversation_id: str, req: ExpressRequirement,
                             reply: str, plan_id: str | None,
                             sync: dict | None = None) -> None:
    """快速通道结束后把上下文一次性写入 LangGraph thread（短期记忆）。
    快速通道绕过状态机，thread 中缺少行程相关状态；同步完成后用户后续调整需求可直接在状态机续接，无需重新提问。
    必须一次性写入全部数据，同一thread多次调用aupdate_state会触发Ambiguous update异常。
    调用时机：清单落库完成后，此时行程数据才完整可用。
    """
    import uuid as _uuid
    from langchain_core.messages import AIMessage, HumanMessage
    from app.agents.handoffs.travel_agent import create_travel_agent
    from app.models.base import async_session_maker
    from app.services.itinerary_plan_service import get_plan

    itinerary_list: list = []
    if plan_id:
        async with async_session_maker() as db:
            plan = await get_plan(db, plan_id, _uuid.UUID(conversation_id))
        itinerary_list = (plan.plan_data.get("itinerary_list") if plan else None) or []

    agent = await create_travel_agent()
    # 提高递归上限，防止长回合被默认25上限截断
    config = {"configurable": {"thread_id": conversation_id}, "recursion_limit": 60}
    requirement = {
        "departure_city": req.departure_city,
        "destination": req.destination,
        "departure_date": req.departure_date,
        "travel_days": req.travel_days,
        "adult_count": req.adult_count,
        "children_count": req.children_count,
        "budget_min": float(req.budget_max or 0),
        "budget_max": float(req.budget_max or 0),
        # 档位口径与 state_transition 的三档同源：express 只有"人均预算上限"一个数，
        #   以其同时充当上下限（min=max），再套同一张阈值表（<3000 经济 / <8000 舒适 / 否则高端）
        "budget_level": ("economy" if (req.budget_max or 0) < 3000
                         else "comfort" if (req.budget_max or 0) < 8000 else "luxury"),
        "travel_styles": req.travel_styles,
        "special_needs": "",
    }
    # 与 _select_choices 同口径：按勾选顺序取首个已知模式（不再默认 train 优先）
    transport = next((m for m in req.transport_types if m in {"train", "flight", "driving"}), "driving")
    updates = {
        "messages": [
            HumanMessage(content=(
                f"[{'自动确认表单' if req.auto_confirm else '多目的地行程'}] "
                f"目的地 {req.destination}，{req.departure_date} 出发，"
                f"{req.travel_days} 天，{req.adult_count + req.children_count} 人，"
                f"人均预算≤{req.budget_max or '不限'}元"
            )),
            AIMessage(content=reply[:2000]),  # 规划结果摘要，避免checkpoint数据膨胀
        ],
        "user_id": "",
        "auto_confirm": True,
        "user_requirement": requirement,
        "selected_destination": req.destination,
        "selected_transport": transport,
        "selected_accommodation_types": req.accommodation_types or ["economy_hotel"],
        "selected_food_types": req.food_preferences or ["local", "specialty"],
        "itinerary": itinerary_list,
        "current_step": "budget_summarization",
    }
    if sync:
        sel = sync.get("sel") or {}
        updates["leg_prices"] = {
            f"{tr['src']}→{tr['dst']}": float(tr["price"])
            for tr in (sel.get("transports") or []) if float(tr.get("price") or 0) > 0
        }
        updates["stay_prices"] = {
            h["city"]: float(h["price"])
            for h in (sel.get("hotels") or []) if float(h.get("price") or 0) > 0
        }
        updates["itinerary_body"] = sync.get("itinerary_body") or []
        if sync.get("breakdown"):
            updates["budget"] = sync["breakdown"]
    # 使用异步aupdate_state，同步update_state会与AsyncPostgresSaver产生调用异常
    await agent.aupdate_state(config, updates)
    app_logger.info("[express] 状态机短期记忆已同步（含 itinerary/预算账本，current_step=budget_summarization）")

