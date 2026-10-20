"""对话流式执行器：LangGraph 框架流式"""
import asyncio
import json
import re
from datetime import date, timedelta

from langchain_core.messages import HumanMessage

from app.core.events import make_event, ROLE_ASSISTANT, ROLE_ASSISTANT_RESET
from app.core.intent import detect_intent
from app.core.event_stream import EventStream
from app.agents.handoffs.travel_agent import create_travel_agent, get_llm
from app.utils.logger import app_logger

async def _stream_plain(content: str, stream: EventStream) -> str:
    """闲聊路径：直连 LLM 流式，不走规划 Agent"""
    chunks: list[str] = []
    async for chunk in get_llm().astream([HumanMessage(content=content)]):
        if chunk.content:
            chunks.append(chunk.content)
            stream.push(make_event(ROLE_ASSISTANT, chunk.content))
    return "".join(chunks)


# 交通步骤权威班次清单——由代码直出，不由模型自由生成
_TICKET_SEG_LIMIT = 10
_TICKET_SEAT_ORDER = ("二等座", "硬座", "一等座", "硬卧", "软卧", "无座")
# 高铁/动车车次前缀（G 高铁 / C 城际 / D 动车），用于剔除段内混入的普速
_HS_PREFIX = ("G", "C", "D")


def _row_seat_label(row: dict) -> str:
    """取一行班次的票价文案：紧凑行直接用；原始行从席别字典里按常用度挑一个"""
    if row.get("seat"):
        return row["seat"]
    seats = row.get("seats") or {}
    for k in _TICKET_SEAT_ORDER:
        if k in seats:
            status, price = seats[k]
            return f"{k} {status} {price:g}元"
    return "票价未返回"


def _rows_to_seg(rows: list, seg: dict | None = None) -> dict:
    """ 把一趟查询的班次行归成 {车次: (发车, 车次, 到达, 历时, 票价文案)}

    从 _ticket_table_from_tail 抽出的公共归并逻辑（同车次只留发车最早的一条），
    供"本轮工具返回"与"直查通道的单次 12306 返回"共用，避免两处去重口径分叉；
    seg 传入时按同一规则合并进去（多段/多轮查询共用一张表）
    """
    seg = {} if seg is None else seg
    for r in rows:
        prev = seg.get(r["code"])
        # ★ 同车次只留发车最早的一条（直查与交通步共用这一处去重口径）
        if prev and prev[0] <= r["dep"]:
            continue
        seg[r["code"]] = (r["dep"], r["code"], r["arr"], r["dur"], _row_seat_label(r))
    return seg


def _hs_only(seg: dict) -> dict:
    """ 段内出现 G/C/D 即剔除混入的普速（K/T/Z/纯数字）；整段只有普速则原样保留"""
    if any(v[1][:1] in _HS_PREFIX for v in seg.values()):
        return {k: v for k, v in seg.items() if v[1][:1] in _HS_PREFIX}
    return seg

def _render_ticket_table(segs: dict, only_leg: str = "", header: str = "", tail: str = "") -> str:
    """把「段 → 班次」渲染成 Markdown 表，经过普速过滤和段筛选；无数据返回空串。
    header/tail 可自定义表头与结尾文案。直查通道面向用户时，可替换原本给模型的约束提示，避免内部提示展示在前端。
    """
    segs = {k: _hs_only(v) for k, v in segs.items()}
    if only_leg and "→" in only_leg:
        a, b = only_leg.split("→")
        segs = {
            (f, t): v for (f, t), v in segs.items()
            if (a in f or f in a) and (b in t or b in t)
        }
    if not segs:
        return ""
    lines = [
        "\n\n---\n",
        header or "**以下为系统直出的 12306 真实班次（每段以本表为准，表外车次为无效信息，请勿采纳）**\n",
    ]
    total = 0
    for (frm, to), seg in segs.items():
        items = sorted(seg.values())[:_TICKET_SEG_LIMIT]
        lines.append(f"\n### {frm} → {to}\n\n")
        lines.append("| 车次 | 出发→到达 | 历时 | 票价 |\n|---|---|---|---|\n")
        for dep, code, arr, dur, seat in items:
            lines.append(f"| {code} | {dep}→{arr} | {dur} | {seat} |\n")
        total += len(items)
    if tail:
        lines.append(tail)
    elif only_leg:
        lines.append(f"\n请从以上**真实班次**中选择「{only_leg}」这一段的班次。\n")
    else:
        lines.append("\n请从以上**真实班次**中选择各段班次。\n")
    app_logger.info(f"✅ 直出真实班次清单：{len(segs)} 段 / {total} 趟（表外车次均视为编造）")
    return "".join(lines)


def _ticket_table_from_tail(tail: list, only_leg: str = "") -> str:
    """将本轮查询得到的真实班次按路段分组生成Markdown表格，查询几段就输出几张表。
    兼容12306原始返回格式，起到双保险作用。
    无真实数据返回空字符串，不编造车次。
    only_leg不为空时，仅渲染当前待确认路段；城市名采用宽容匹配，支持站名带有北站/西站等后缀场景。
    """
    from app.tools.mcp_tools import parse_compact_rows, parse_ticket_rows

    segs: dict[tuple, dict] = {}
    for m in tail:
        if getattr(m, "type", None) != "tool":
            continue
        content = getattr(m, "content", "")
        rows = parse_compact_rows(content) or parse_ticket_rows(content)
        if not rows:
            continue
        # 一次工具调用对应一个路段，按首趟车次起讫站分组；同路段多次查询自动合并去重
        _rows_to_seg(rows, segs.setdefault((rows[0]["from"], rows[0]["to"]), {}))
    return _render_ticket_table(segs, only_leg)


# ===== 旅行域纯查询直通：单点事实查询不走状态机 =====
# 用于识别单独查高铁/航班的请求，直接调用MCP工具并由代码渲染班次表，不读写状态机
from app.core.intent import DAY_OFFSET as _DAY_OFFSET

_FLIGHT_LIMIT = 10


def _render_flight_table(rows: list[dict]) -> str:
    """航班清单渲染：仅渲染解析出的真实航班，禁止模型编造"""
    if not rows:
        return ""
    lines = [
        "\n\n---\n",
        "**以下为系统直出的实时航班（每班以本表为准，表外航班为无效信息）**\n", "\n",
        "| 航班 | 出发→到达 | 航司 | 准点率 | 航站楼 | 价格 |\n|---|---|---|---|---|---|\n"
    ]
    for r in rows[:_FLIGHT_LIMIT]:
        stop = f"（经停{r['stop']}）" if r.get("stop") else ""
        # 仅使用航班号匹配到的真实票价，无匹配则显示占位符
        price = f"{r['price']:.0f}元" if r.get("price") else "—"
        lines.append(
            f"| {r['code']}{stop} | {r['dep']}→{r['arr']} | {r['airline'][:8]} | "
            f"{r['ontime'] or '—'} | {r['terminal'] or '—'} | {price} |\n"
        )
    lines.append("\n以上为实时航班时刻，票价与余座以航空公司官方渠道为准。\n")
    app_logger.info(f"✅ 直出真实航班清单：{len(rows)} 班（表外航班均视为编造）")
    return "".join(lines)


async def _flight_table(frm: str, to: str, day: str) -> str:
    """航班直查：城市→机场编码→调用VariFlight工具，代码渲染表格。无数据返回空串，不编造航班"""
    from app.tools.mcp_tools import (_CITY_AIRPORT, _CITY_IATA, flight_price_args,
                                     get_flight_tools, parse_flight_prices, parse_flight_rows)
    # 时刻表使用机场码，票价接口使用城市码，两套编码不可混用
    dep, arr = _CITY_AIRPORT.get(frm), _CITY_AIRPORT.get(to)
    if not dep or not arr:
        return ""

    tools = await get_flight_tools()
    tool = next((t for t in tools if t.name.lower() == "searchflightsbydeparr"), None)
    if tool is None:
        return ""

    ptool = next((t for t in tools if t.name.lower() == "getflightpricebycities"), None)
    cdep, carr = _CITY_IATA.get(frm), _CITY_IATA.get(to)

    async def _prices() -> str:
        """单独查询票价，票价失败仅价格列置空，不影响航班基础信息"""
        if ptool is None or not (cdep and carr):
            app_logger.info(f"[直查] 航班票价跳过：ptool={bool(ptool)} 城市码={cdep}/{carr}")
            return ""
        try:
            return await ptool.ainvoke(flight_price_args(ptool, day, cdep, carr))
        except Exception:
            app_logger.warning("[直查] 航班票价查询失败，本次表内价格留空", exc_info=True)
            return ""

    try:
        raw, price_raw = await asyncio.gather(
            tool.ainvoke({"date": day, "dep": dep, "arr": arr}), _prices())
    except Exception:
        app_logger.warning("[直查] 航班查询失败", exc_info=True)
        return ""

    rows = parse_flight_rows(raw)
    # 按航班号匹配票价，航线最低价不可直接当作单班票价
    by_flight = parse_flight_prices(price_raw)[0] if price_raw else {}
    for r in rows:
        r["price"] = float(by_flight.get(str(r.get("code", "")).upper()) or 0)
    return _render_flight_table(rows) if rows else ""


# 直查多轮槽位补全：缓存待收集的起讫城市/日期，最多 _PENDING_TTL 轮，过期释放
_PENDING_QUERY: dict[str, dict] = {}
_PENDING_TTL = 2


def _cities_in(text: str) -> list[str]:
    """按原文出现顺序提取文本内匹配到的城市名称"""
    try:
        from app.data.cities import CITY_PROVINCE
        return sorted({n for n in CITY_PROVINCE if n in text}, key=text.index)
    except Exception:
        return []


_MONTH_DAY_RE = re.compile(r"\d{1,2}\s*月\s*\d{1,2}\s*[日号]")


def _fill_slots(slot: dict, text: str, intent) -> None:
    """填充查询槽位；已存在字段不会被覆盖，一次性完整起讫对可直接覆盖"""
    pair = _parse_city_pair(text)
    if pair:
        slot["frm"], slot["to"] = pair
    else:
        cities = _cities_in(text)
        if cities:
            city = cities[0]
            if "出发" in text or f"从{city}" in text:
                slot["frm"] = city
            elif not slot.get("to"):
                slot["to"] = city
            elif not slot.get("frm"):
                slot["frm"] = city
    if intent.date_word or _MONTH_DAY_RE.search(text):
        slot["date"] = _parse_query_date(text)
    if intent.flight_object:
        slot["kind"] = "flight"
    elif intent.train_object:
        slot["kind"] = "train"


def _missing_slot_text(slot: dict) -> str:
    """根据缺失的槽位生成固定追问文本，不调用LLM"""
    kind = "航班" if slot.get("kind") == "flight" else "班次"
    frm, to = slot.get("frm"), slot.get("to")
    if frm and not to:
        return f"好的，从 **{frm}** 出发。您要到哪个城市？告诉我目的地（和日期）我就直接查{kind}。"
    if to and not frm:
        return f"好的，到 **{to}**。您从哪个城市出发？告诉我出发地（和日期）我就直接查{kind}。"
    return (f"您是想查{kind}吗？只要给我**出发地、到达地和日期**就行，"
            f"例如：「查明天福州到深圳的高铁」。")


def _parse_city_pair(text: str) -> tuple[str, str] | None:
    """解析起讫城市：优先城市库匹配，不足再用正则匹配「X到Y」格式"""
    names: list[str] = []
    try:
        from app.data.cities import CITY_PROVINCE
        names = sorted({n for n in CITY_PROVINCE if n in text}, key=text.index)
    except Exception:
        names = []
    if len(names) >= 2:
        return names[0], names[1]
    m = re.search(r"([\u4e00-\u9fa5]{2,4})\s*(?:到|去|往|至|→)\s*([\u4e00-\u9fa5]{2,4})", text)
    return (m.group(1), m.group(2)) if m else None


def _parse_query_date(text: str) -> str:
    """解析查询日期：优先月日，再相对日期词，无匹配默认取今日"""
    from datetime import date as _date, timedelta as _td
    today = _date.today()
    m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", text) or re.search(
        r"(\d{1,2})\s*[-/]\s*(\d{1,2})", text
    )
    if m:
        try:
            return _date(today.year, int(m.group(1)), int(m.group(2))).isoformat()
        except ValueError:
            pass
    for word, off in _DAY_OFFSET:
        if word in text:
            return (today + _td(days=off)).isoformat()
    return today.isoformat()


async def try_direct_ticket_query(content: str, stream=None,
                                  conversation_id: str = "") -> str | None:
    """命中交通班次单点查询（高铁/航班）则返回代码渲染的真实班次清单
    确定性门禁规则：
    ① 含票务对象 +（查询动词/日期词）；② 不含规划词汇；
    ③ 可解析起讫城市；④ 对应工具已加载。任一不满足返回 None，交由原通道处理
    """
    intent = detect_intent(content)
    slot = _PENDING_QUERY.get(conversation_id or "")

    # 存在待补槽位，但用户输入包含规划类内容，清空查询状态
    if slot and intent.planning_noise:
        _PENDING_QUERY.pop(conversation_id or "", None)
        slot = None

    if not slot and not intent.looks_like_query:
        app_logger.info(f"[direct] 门禁①②未命中，交回原通道：{content[:30]}")
        return None

    slot = slot or {"kind": "flight" if intent.flight_object else "train"}
    _fill_slots(slot, content, intent)

    # 起讫城市缺失，追问补齐槽位，超过轮数上限则退出直查通道
    if not (slot.get("frm") and slot.get("to")):
        slot["turns"] = slot.get("turns", 0) + 1
        if conversation_id and slot["turns"] <= _PENDING_TTL:
            _PENDING_QUERY[conversation_id] = slot
            app_logger.info(f"[direct] 槽位待补（第 {slot['turns']} 轮）：{slot}")
        else:
            _PENDING_QUERY.pop(conversation_id or "", None)
            return None
        text = _missing_slot_text(slot)
        if stream is not None:
            stream.push(make_event(ROLE_ASSISTANT, text))
        return text

    # 槽位收集完成，清除待查询缓存
    _PENDING_QUERY.pop(conversation_id or "", None)
    frm, to, day = slot["frm"], slot["to"], slot.get("date") or _parse_query_date(content)

    def _emit(text: str) -> str:
        """统一流式事件推送，所有分支出口统一调用，防止事件漏推"""
        if stream is not None:
            stream.push(make_event(ROLE_ASSISTANT, text))
        return text

    _note = ""
    # 航班查询分支，无直飞班次自动降级查询同日高铁
    if intent.flight_object:
        if stream is not None:
            stream.push(make_event(ROLE_ASSISTANT, f"✈️ 正在为您查询 {frm} → {to} 的航班，请稍候…\n"))
        _ftable = await _flight_table(frm, to, day)
        if _ftable:
            app_logger.info(f"✅ 直查通道（航班）：{day} {frm}→{to}（代码渲染，未走状态机）")
            return _emit(f"为您查询到 **{day}** {frm} → {to} 的航班：{_ftable}")
        _note = (f"未查询到 **{day}** {frm} → {to} 的直飞航班（航班数据源该航线暂无直飞班次），"
                 "为您附上同期高铁班次供参考：\n\n")

    # 高铁查询分支
    try:
        from app.tools.mcp_tools import get_train_tools, parse_compact_rows, parse_ticket_rows
        tools = await get_train_tools()
    except Exception:
        return None

    tool = next((t for t in tools if t.name.lower() == "get-tickets"), None)
    if tool is None:
        return None

    if stream is not None:
        stream.push(make_event(ROLE_ASSISTANT, f"🚄 正在为您查询 {frm} → {to} 的高铁班次，请稍候…\n"))
    try:
        raw = await tool.ainvoke({"date": day, "fromStation": frm, "toStation": to})
    except Exception:
        app_logger.warning("[直查] 12306 班次查询失败", exc_info=True)
        return _emit(_note + f"抱歉，**{day} {frm} → {to}** 的实时班次查询失败，请稍后重试或换一站查询。")

    rows = parse_compact_rows(raw) or parse_ticket_rows(raw)
    if not rows:
        return _emit(_note + f"没有查到 **{day} {frm} → {to}** 的可用班次（12306 未返回余票数据），"
                             "您可以换一个日期或邻近车站再试。")

    table = _render_ticket_table(
        {(rows[0]["from"], rows[0]["to"]): _rows_to_seg(rows)},
        header="**以下为 12306 实时班次（数据来源：12306，余票与票价随时变动）**\n",
        tail="\n以上为查询结果，如需预订请在 12306 官方渠道下单。\n",
    )
    if not table:
        return None

    app_logger.info(f"✅ 直查通道：{day} {frm}→{to} 共 {len(rows)} 趟（代码渲染，未走状态机）")
    return _emit(_note + f"为您查询到 **{day}** {frm} → {to} 的班次：{table}")

_WEATHER_CITY_MAX = 3  # 逐城展示上限，防止多城串行拖慢关键路径
def _query_fallback_text(content: str, intent) -> str:
    """闲聊通道识别到交通查询意图时的确定性澄清文案，不调用模型、不编造数据。
    触发场景：存在交通相关意图，但缺少必要查询参数，例如日期、起讫城市。
    只向用户补齐缺失槽位，不直接查询。
    """
    kind = "航班" if (intent.flight_object and not intent.train_object) else "班次"
    pair = _parse_city_pair(content)
    if pair:
        frm, to = pair
        # 成功解析出城市对，仅缺少日期，引导用户补充日期信息
        return (f"您是想查 **{frm} → {to}** 的{kind}吗？"
                f"告诉我日期我就直接给您真实数据，例如「明天」或「10月1日」。")
    return (f"您是想查{kind}吗？我可以直接查 12306 的实时班次与航班时刻，"
            f"只要给我**出发地、到达地和日期**就行，例如：「查明天福州到深圳的高铁」。")

async def try_weather_query(content: str, stream=None, intent=None) -> str | None:
    """闲聊通道识别到天气查询意图时的确定性处理：代码直查高德天气并渲染，不经模型转述。
    命中意图但缺失城市时进行追问；查询失败如实返回，不编造数据；未命中天气意图返回 None。
    intent 可由外部传入避免重复计算，不传则函数内部自行执行意图检测。
    """
    intent = intent or detect_intent(content)
    if not intent.weather_object:
        return None
    cities = _cities_in(content)
    if not cities:
        _ask = ("您想查哪座城市的天气？告诉我城市名就行，"
                "例如：「明天福州天气怎么样」。")
        if stream is not None:
            stream.push(make_event(ROLE_ASSISTANT, _ask))
        return _ask
    try:
        from app.tools.mcp_tools import get_weather_tools
        from app.agents.routers.destination_router import WeatherForecast, _normalize_weather_payload
        tools = await get_weather_tools()
        tool = next((t for t in tools if t.name == "get_weather_forecast"), None)
        if tool is None:
            raise RuntimeError("天气工具未加载")
    except Exception as e:
        app_logger.warning(f"[weather] 天气工具未就绪：{type(e).__name__}: {e}")
        _fail_text = (f"抱歉，暂时没拿到 **{'、'.join(cities[:_WEATHER_CITY_MAX])}** 的天气数据，"
                      "请稍后再问，或换个更规范的城市名。")
        if stream is not None:
            stream.push(make_event(ROLE_ASSISTANT, _fail_text))
        return _fail_text

    # 识别查询日期，存在日期词则只返回目标日期预报，否则返回全部预报
    asked_day = _parse_query_date(content) if (
        intent.date_word or _MONTH_DAY_RE.search(content)) else ""
    blocks: list[str] = []
    failed: list[str] = []

    # 多城市循环查询，受最大城市数量限制；逐个查询并流式推送
    for city in cities[:_WEATHER_CITY_MAX]:
        try:
            payload = _normalize_weather_payload(await tool.ainvoke({"city": city}))
            if payload.get("error"):
                raise RuntimeError(str(payload["error"]))
            casts = WeatherForecast.model_validate(payload).casts
            if not casts:
                raise RuntimeError("天气返回为空")
        except Exception as e:
            app_logger.warning(f"[weather] 查询失败（不编造）：{city} | {type(e).__name__}: {e}")
            failed.append(city)
            continue
        note = ""
        if asked_day:
            picked = [c for c in casts if c.date == asked_day]
            if picked:
                casts = picked
            else:
                note = f"（**{asked_day}** 超出预报范围（仅支持未来 4 天），以下为近期天气）\n"
        lines = [f"- {c.date}：白天{c.dayweather}，气温{c.nighttemp}~{c.daytemp}°C，"
                 f"{c.daywind}风 {c.daypower} 级" for c in casts]
        block = note + f"**{city}** 天气（数据来源：高德天气）：\n" + "\n".join(lines)
        blocks.append(block)
        if stream is not None:
            stream.push(make_event(ROLE_ASSISTANT, ("\n\n" if len(blocks) > 1 else "") + block))
    if not blocks:
        _fail_text = (f"抱歉，暂时没拿到 **{'、'.join(failed)}** 的天气数据，"
                      "请稍后再问，或换个更规范的城市名。")
        if stream is not None:
            stream.push(make_event(ROLE_ASSISTANT, _fail_text))
        return _fail_text
    text = "\n\n".join(blocks)
    if failed:
        _fail_note = f"（**{'、'.join(failed)}** 暂时没查到）"
        text += f"\n\n{_fail_note}"
        if stream is not None:
            stream.push(make_event(ROLE_ASSISTANT, f"\n\n{_fail_note}"))

    # 存在额外问题时调用模型补充回答，天气基础数据由代码直接输出
    if intent.extra_ask or (content.count("?") + content.count("？")) >= 2:
        extra = await _weather_extra_answer(content)
        if extra:
            text = f"{text}\n\n{extra}"
            if stream is not None:
                stream.push(make_event(ROLE_ASSISTANT, f"\n\n{extra}"))
    return text
# 目的地步天气"归代码"——事实数字不经过模型转述。
# 代码直查并渲染天气，同时剔除模型自写的天气相关文本与工具占位语。
_WEATHER_DROP_RE = re.compile(
    r"℃|°C|天气概况|实时天气|天气信息|请稍等|请稍候|我将查询|正在查询|稍后为您"
)
def _strip_model_weather(text: str) -> str:
    """剔除模型自写的天气行与工具往返占位语（保留城市/景点/美食等其余内容）"""
    kept = [ln for ln in (text or "").splitlines() if not _WEATHER_DROP_RE.search(ln)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()

async def _weather_extra_answer(content: str) -> str:
    """补答天气问句里"天气之外"的诉求；无其余诉求、或调用失败时返回空串（绝不影响天气段）"""
    prompt = (
        "用户问题里的天气部分已由系统用权威数据回答完毕。"
        "请只回答其中**与天气无关**的其它诉求（如景点怎么玩、美食推荐、行程安排）；"
        "不要再输出任何天气、气温、降雨相关内容。若没有其它诉求，只回复四个字：无需补充。\n\n"
        f"用户问题：{content}"
    )
    try:
        resp = await get_llm().ainvoke([HumanMessage(content=prompt)])
    except Exception as e:
        app_logger.warning(f"[weather] 补充回答失败（不影响天气段）：{type(e).__name__}: {e}")
        return ""
    text = _strip_model_weather(str(getattr(resp, "content", "") or "")).strip()
    if not text or (len(text) <= 12 and "无需补充" in text):
        return ""
    return text

# 协议泄漏过滤：拦截模型输出内部工具协议标记，防止污染对话历史，避免输出中断
_LEAK_HINTS = ("✿", "你可以在回复中插入以下命令", "✿FUNCTION✿", "✿ARGS✿")

def _is_protocol_leak(text: str) -> bool:
    """判断流式文本片段是否包含模型内部工具协议泄漏标记"""
    return any(h in text for h in _LEAK_HINTS)

# ===== 系统指令/工具说明泄漏句清洗 =====
# 拦截模型将系统提示、工具使用规范原样输出到用户正文的情况，支持单句过滤
_LEAK_SENTENCE_HITS = (
    "将json格式结果转换为用户可读的信息",
    "请记住，只有在收集到足够信息后才能调用工具",
    "当需要调用工具时，你必须以如下格式输出",
    "以如下格式输出",
    "请勿修改或删除上方已定义好的函数",
    "请确保调用的工具名称和参数正确",
    "使用这些工具可以帮助你更好地完成任务",
    "请根据你的判断决定是否调用工具",
)

# 正则匹配工具调用痕迹，拦截工具名称、参数JSON片段泄漏
_LEAK_TOOL_TRACE_RE = re.compile(r"_tool|我将使用以下参数调用|我将调用工具|\[\{")

# 块级泄漏起始标记，用于识别整块工具说明、参数列表区域
_LEAK_BLOCK_START = ("## 注意：", "## 注意:", "## 注意事项：", "## 注意事项:")

def _is_trace_block_start(line: str) -> bool:
    """判断该行是否为工具泄漏块的起始行"""
    return any(line.startswith(m) for m in _LEAK_BLOCK_START) or bool(
        _LEAK_TOOL_TRACE_RE.search(line)
    )

def _strip_leak_blocks(text: str) -> str:
    """整段移除系统指令块与工具参数块，正常正文不受影响；无命中直接返回原文"""
    if not text:
        return text
    lines = text.splitlines(keepends=True)
    if not any(_is_trace_block_start(line.strip()) for line in lines):
        return text
    out: list[str] = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        if not skipping:
            if _is_trace_block_start(stripped):
                skipping = True
                continue
            out.append(line)
            continue
        # 处于丢弃状态：空行、列表项、编号行全部丢弃；遇到普通正文行则退出丢弃模式
        if not stripped or stripped[0] in "-*·" or stripped[:1].isdigit():
            continue
        skipping = False
        out.append(line)
    return "".join(out)

def _is_leak_sentence(sentence: str) -> bool:
    """判断单句是否属于泄漏内容：匹配预设泄漏句式，或包含工具调用痕迹"""
    return (
        any(h in sentence for h in _LEAK_SENTENCE_HITS)
        or bool(_LEAK_TOOL_TRACE_RE.search(sentence))
    )


# ===== 清单承诺话术清洗：清单状态由代码事实控制，禁止模型自行宣称 =====
# 移除模型擅自输出的清单生成类承诺文案，保证前端清单UI状态和文本描述保持一致
_CLAIM_SENTENCE_RE = re.compile(
    r"行程清单(已生成|生成完毕|已为您生成|已上传)"
    r"|(已|已经)(按此方案|为您)?生成.{0,8}行程清单"
    r"|(您)?(可以|可在|请)在下方(查看|查看详情|点击查看)"
)

def _strip_claim_sentences(text: str) -> str:
    """剔除模型自行宣称「清单已生成、可在下方查看」类话术，清单状态以清单帧为准"""
    if not text:
        return text
    sentences = re.split(r"(?<=[。！？!?\n])", text)
    kept = [s for s in sentences if not _CLAIM_SENTENCE_RE.search(s)]
    return "".join(kept) if len(kept) != len(sentences) else text

# ===== 收尾句去阻塞改写 =====
# 将确认类收尾问句替换为引导调整的友好文案，避免重复确认造成用户困惑
_INVITE_BLOCK_RE = re.compile(
    r"请确认(以上|上述|此)[^。！？!?\n]{0,14}(是否符合您的需求|是否满足(您的)?(需求|要求)|是否合适|是否满意|是否需要调整)[。！!？]?"
    r"|(以上|上述)(的)?(行程|方案|安排)[^。！？!?\n]{0,10}是否符合您的需求[。！!？]?"
    r"|是否(需要|要)(进一步)?调整(一下)?[吗呢]?[？?](?=$|\n)"
)
_INVITE_TEXT = "如需调整（天数/预算/目的地等），直接告诉我即可。"

def _rewrite_closing_invite(text: str) -> str:
    """将阻塞式确认问句改写为无阻塞邀请语句"""
    return _INVITE_BLOCK_RE.sub(_INVITE_TEXT, text) if text else text

def _scrub_leak_text(text: str) -> str:
    """清洗回复文本：移除系统指令、工具参数泄漏、虚假清单承诺，改写确认类收尾句。
    执行顺序：块级清洗 → 清单承诺句子剔除 → 收尾句改写 → 单句泄漏过滤。
    无泄漏内容时原样返回；调用方可通过返回值对比判断是否需要重置推送。
    """
    if not text:
        return text
    stripped = _strip_leak_blocks(text)
    # 剔除清单相关虚假承诺话术
    stripped = _strip_claim_sentences(stripped)
    # 改写确认类结尾句子
    stripped = _rewrite_closing_invite(stripped)
    # 分句过滤单句泄漏
    sentences = re.split(r"(?<=[。！？!?\n])", stripped)
    kept = [s for s in sentences if not _is_leak_sentence(s)]
    return "".join(kept) if len(kept) != len(sentences) else stripped


# ===== 同回合内长正文去重 =====
# 去除模型重复粘贴的完整行程正文与预算表；短文本不参与去重，无重复直接返回原文
_DEDUPE_MIN_LEN = 200

def _dedupe_texts(rounds: list[str]) -> list[str]:
    """轮次文本去重，支持两种重复场景：
    1. 两段文本完全相同
    2. 后一段为完整版，前一段是截断版本（后段包含前段）
    长文本（≥200字）判定子串重复，保留时间靠后的完整版；短文本不参与去重。
    """
    kept: list[str] = []
    kept_long: list[str] = []
    for t in reversed(rounds):
        drop = False
        if len(t) >= _DEDUPE_MIN_LEN:
            for p in kept_long:
                if p in t or t in p:
                    drop = True
                    break
        if not drop:
            kept.append(t)
            if len(t) >= _DEDUPE_MIN_LEN:
                kept_long.append(t)
    kept.reverse()
    return kept

# ===== 散文段落换措辞重述折叠 =====
# 对普通散文段落做相似度去重；表格、列表、编号行不参与去重
_DEDUPE_PARA_MIN = 25
_DEDUPE_COMMON = 14

def _norm_para(text: str) -> str:
    return re.sub(r"\s+", "", text or "")

def _is_prose_para(para: str) -> bool:
    """判断是否为普通散文段落；列表、表格、标题行不参与折叠"""
    p = para.strip()
    return bool(p) and p[0] not in "-|*#·•0123456789"

def _common_len(a: str, b: str) -> int:
    from difflib import SequenceMatcher
    m = SequenceMatcher(None, a, b).find_longest_match(0, len(a), 0, len(b))
    return m.size

# 预告等待话术正则，同一回合只保留一次预告
_NOTICE_RE = re.compile(r"(请稍等|稍等片刻|请稍候|正在为您|马上为您|我将计算|我将为您)")

def _fold_restatements(text: str, state: dict) -> str:
    """移除重复重述散文段落与重复等待预告语。
    state：跨段落共享状态，保存已见过的散文段落、预告标记。
    表格/列表/编号行全部保留，不参与折叠。
    """
    seen: list[str] = state.setdefault("paras", [])
    kept: list[str] = []
    for para in re.split(r"\n\s*\n", text or ""):
        key = _norm_para(para)
        prose = bool(key) and _is_prose_para(para)
        is_notice = prose and bool(_NOTICE_RE.search(para))

        if len(key) >= _DEDUPE_PARA_MIN and prose:
            if any(_common_len(key, s) >= _DEDUPE_COMMON for s in seen):
                app_logger.info(f"去重：删除换措辞重述段（{len(key)} 字）")
                continue
            seen.append(key)
        elif is_notice and state.get("notice"):
            app_logger.info(f"去重：删除重复预告语（{len(key)} 字）")
            continue
        if is_notice:
            state["notice"] = True
        kept.append(para)
    return "\n\n".join(kept)

def _chunk_has_tool_call(chunk) -> bool:
    """判断流式块是否携带工具调用增量，用于识别工具执行轮"""
    if getattr(chunk, "tool_call_chunks", None):
        return True
    kwargs = getattr(chunk, "additional_kwargs", None) or {}
    return bool(kwargs.get("tool_calls"))

async def _stream_agent(
        agent, input_data: dict, config: dict, stream: EventStream,
        buffer_all: bool = False,
        defer_push: bool = False,
) -> str:
    """框架流式：astream_events 捕获 LLM token，工具调用由 Agent 自主决策
    buffer_all=True（交通规划步骤）：全程只缓冲不逐字推，回合末一次性推送
    完整拼接。用于高铁/航班类班次巨多、逐字流式渲染缓慢的步骤。
    """
    rounds: list[str] = []        # 每一轮的完整文本
    round_buf: list[str] = []     # 当前轮的 token 缓冲
    pushed: list[str] = []        # 已实时推送给前端的 token
    round_meta: list[tuple[int, bool]] = []   # 诊断：每轮 (字数, 是否工具轮)
    first_round_done = False      # 首轮是否已结束
    round_had_tool = False        # 当前轮是否出现工具调用
    leaky = False                 # 是否检测到协议泄漏句（需回合末复位重推）
    seen_state: dict = {"paras": [], "notice": False}   # 跨轮折叠状态（重述段 / 预告语）

    async for event in agent.astream_events(input_data, config=config, version="v2"):
        etype = event.get("event")
        if etype == "on_chat_model_stream":
            chunk = event.get("data", {}).get("chunk")
            if chunk is None:
                continue
            if _chunk_has_tool_call(chunk):
                round_had_tool = True
            content = getattr(chunk, "content", None)
            if content:
                if _is_protocol_leak(content):
                    continue
                round_buf.append(content)
                if first_round_done and not buffer_all:
                    pushed.append(content)
                    stream.push(make_event(ROLE_ASSISTANT, content))
        elif etype == "on_chat_model_end":
            text = "".join(round_buf)
            round_buf.clear()
            cleaned = _scrub_leak_text(text)
            cleaned = _fold_restatements(cleaned, seen_state)
            if cleaned != text:
                leaky = True
            if cleaned.strip():
                rounds.append(cleaned)
            round_meta.append((len(text), round_had_tool))
            round_had_tool = False
            first_round_done = True

    # 兜底处理未收到 end 事件的残留片段
    if round_buf:
        text = "".join(round_buf)
        cleaned = _scrub_leak_text(text)
        cleaned = _fold_restatements(cleaned, seen_state)
        if cleaned != text:
            leaky = True
        if cleaned.strip():
            rounds.append(cleaned)
            round_meta.append((len(cleaned), round_had_tool))

    rounds = _dedupe_texts(rounds)
    if not rounds:
        return ""

    if defer_push:
        return "".join(rounds)

    shown = "".join(pushed)
    app_logger.info(f"🧩 轮次明细（字数, 是否工具轮）：{round_meta}，已推送 {len(shown)} 字")

    # 出口1：全程没有实时推送，回合末整块推送
    if not shown:
        text = "".join(rounds)
        stream.push(make_event(ROLE_ASSISTANT, text))
        app_logger.info(f"🔇 首轮未实时推送，回合末补推 {len(text)} 字")
        return text

    # 出口2：检测泄漏/重述，复位并重推清洗后完整文本
    if leaky and shown:
        text = "".join(rounds)
        stream.push(make_event(ROLE_ASSISTANT_RESET))
        stream.push(make_event(ROLE_ASSISTANT, text))
        app_logger.info(f"↩️ 检测到泄漏/重述内容，撤销已推送 {len(shown)} 字，改推清洗后 {len(text)} 字")
        return text

    # 出口3：首轮为空或工具旁白，保留已推送内容，不回退
    if round_meta[0][0] == 0 or round_meta[0][1]:
        app_logger.info(f"✅ 首轮无可保留正文（0 字或工具轮旁白），保留已推送 {len(shown)} 字（不回退，防内容丢失）")
        return shown

    # 出口4：首轮是正文开头但被缓冲，复位并推送完整拼接文本
    text = "".join(rounds)
    stream.push(make_event(ROLE_ASSISTANT_RESET))
    stream.push(make_event(ROLE_ASSISTANT, text))
    app_logger.info(f"↩️ 首轮为正文开头，撤销已推送 {len(shown)} 字，改推完整 {len(text)} 字")
    return text


_ACCOMMODATION_LABELS: dict[str, str] = {
    "star_hotel": "星级酒店",
    "economy_hotel": "经济酒店",
    "hostel": "特色民宿",
    "youth_hostel": "青年旅社",
}

# 各档位常见连锁品牌，仅作参考，不含实时门店与价格
_COMMON_HOTELS: dict[str, list[str]] = {
    "economy_hotel": ["如家", "汉庭", "7 天", "锦江之星", "格林豪泰", "莫泰"],
    "star_hotel": ["希尔顿", "万豪", "洲际", "香格里拉", "凯悦", "雅高"],
    "hostel": ["花间堂", "途家民宿", "小猪民宿", "美团民宿"],
    "youth_hostel": ["国际青年旅舍（YHA）", "万里路青年旅舍", "如家青旅"],
}


def _match_hotel_choice(text: str, requirement: dict | None) -> str:
    """识别用户住宿选择，命中品牌名/委托语返回选中品牌，未命中返回空串
    两类命中：①品牌清单内品牌；②委托语。委托语自动选用户已勾选档位的首个品牌
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    for brand in [b for v in _COMMON_HOTELS.values() for b in v]:
        if brand.replace(" ", "") in raw.replace(" ", ""):
            return brand
    if detect_intent(raw).hotel_accept:
        selected = list((requirement or {}).get("accommodation_types") or [])
        types = [t for t in selected if t in _COMMON_HOTELS] or ["economy_hotel"]
        return _COMMON_HOTELS[types[0]][0]
    return ""


def _build_hotel_fallback(requirement: dict | None, auto_confirm: bool = False,
                          chosen: str = "") -> str:
    """酒店服务不可用时生成降级推荐文案，同时用于流式推送与落库
    自动确认模式不再向用户索要品牌，直接按档位安排；普通模式等待用户选择
    """
    selected = list((requirement or {}).get("accommodation_types") or [])
    types = [t for t in selected if t in _COMMON_HOTELS] or list(_COMMON_HOTELS)
    lines = [
        "\n\n---\n",
        "⚠️ 实时酒店报价服务当前不可用，无法展示具体门店与实时价格。\n",
        "以下按您的住宿类型，列出该档位市面上常见的连锁品牌，供您参考（具体门店与价格到店/官网再定）：\n",
    ]
    for t in types:
        lines.append(f"- **{_ACCOMMODATION_LABELS[t]}**：{'、'.join(_COMMON_HOTELS[t])}\n")
    if chosen:
        lines.append(
            f"✅ 已记录您的住宿选择：**{chosen}**（具体门店与实时价格到店择优选同档位），"
            f"并继续规划后续行程。"
        )
    elif auto_confirm:
        lines.append("已按以上该档位的常见品牌为您直接安排住宿（不指定具体门店与实时价格，到店择优选同档位），并继续规划后续行程。")
    else:
        lines.append("请回复您想选的品牌（或「按推荐来」），我将按该档位继续安排行程。")
    return "".join(lines)


async def _has_hotel_search_tool() -> bool:
    """校验是否存在酒店查询工具；异常时返回False，进入降级兜底"""
    try:
        from app.tools.mcp_tools import get_hotel_tools
        tools = await get_hotel_tools()
    except Exception:
        return False
    return any(
        any(hint in getattr(t, "name", "").lower() for hint in ("searchhotels", "find-hotels"))
        for t in tools
    )

# ===== 交通步骤"清单归代码"——正文里禁止出现任何班次/航班/自驾数字 =====
_TRANSPORT_LIST_RE = re.compile(
    r"[GDCKTZ]\d{1,4}\b"                       # 车次号（G330 / D50 / K507 / T10 / Z162）
    r"|\b[A-Z0-9]{2}\d{3,4}\b"                 # 航班号（CA4105 / 3U8885）
    r"|经济舱|公务舱|头等舱|商务座|一等座|二等座|硬座|硬卧|软卧|机票|航班"
    r"|历时|耗时|过路费|自驾|路程|里程"
    r"|\d+(?:\.\d+)?\s*元"
    # 模型自写的"残行"——只有时刻没有车次号，旧正则认不出来（实测
    # "- 出发-到达时刻：07:21 - 14:31" 三条与代码直出表并存，用户看到两套互相矛盾的班次）
    r"|出发\s*[-—~～至]?\s*到达|到达时刻|发车时刻"
    r"|\d{1,2}:\d{2}\s*[-—~～至]\s*\d{1,2}:\d{2}"
)
_TRANSPORT_HEAD_RE = re.compile(
    r"^#{2,4}\s*[①②③④⑤⑥⑦⑧⑩]"                # #### ① 重庆 → 石家庄
    r"|^#{2,4}\s*交通(方式)?查询结果"
    r"|^\s*[-*•]\s*\*\*(高铁|航班|自驾|火车|飞机)\*\*"
)
_TRANSPORT_LEAD = "已为您查询到以下真实班次，请选择各段的交通方式：\n"
_TRANSPORT_LEAD_MIN = 8


def _zero_leg_guard(vals: dict) -> tuple[bool, str]:
    """ 零段直通判定——出发地与目的地同城（本地游玩）时没有任何城际段

    返回 (是否零段, 出发地城市)。出发地/目的地缺失、或存在城际段时返回 (False, 出发地)，
    单目的地与多目的地均按同一口径（_expected_legs 已按"相邻节点城市不同"计算）
    """
    from app.services.express_planning_service import _split_cities
    from app.tools.state_transition import _expected_legs
    dep = str(((vals.get("user_requirement") or {}).get("departure_city")) or "").strip()
    if not dep or not _split_cities(vals.get("selected_destination") or ""):
        return False, dep
    return (not _expected_legs(vals)), dep  # ★ 段数口径只认第 11 章 _expected_legs


def _leg_context(state: dict) -> tuple[str, int, int]:
    """ 取"当前待确认段" → (段文本, 序号从1起, 总段数)。

    多目的地段级循环专用；单目的地/未进入段级循环时返回 ("", 0, 0)，
    调用方据此走老逻辑，单城行为完全不变
    """
    from app.tools.state_transition import _expected_legs
    try:
        legs = _expected_legs(state)
    except Exception:
        return "", 0, 0
    if not legs:
        return "", 0, 0
    cur = max(0, min(int(state.get("leg_cursor") or 0), len(legs) - 1))
    return legs[cur], cur + 1, len(legs)


def _strip_transport_lists(text: str) -> str:
    """剔除交通步骤正文里模型自编的班次/航班/自驾清单行及其小标题，只留引导语"""
    kept: list[str] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped and (
            _TRANSPORT_LIST_RE.search(stripped) or _TRANSPORT_HEAD_RE.match(stripped)
        ):
            continue
        kept.append(line)
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    return cleaned.replace("以上选项", "以下班次")

# ===== 行程正文归代码渲染（模型只提交结构化数据） =====
# 结构化数据经过Pydantic校验，正文与预算表由本函数确定性拼接。
_BODY_READY_MARK = "天行程结构"
_SLOT_RENDER = (("morning", "上午"), ("noon", "中午"), ("afternoon", "下午"), ("evening", "晚上"))
_INVITE_TAIL = "\n\n如需调整（天数/预算/目的地等），直接告诉我即可。"

def _render_confirmed_block(transport_legs: list | None, hotels: list | None) -> str:
    """已确认的交通与住宿摘要块（数据来自状态机已落库的
    selected_transport_legs / selected_hotels）。两者都空则整块不出——
    本地零段游玩、或品牌兜底缺失时不硬凑。
    """
    legs = [str(x).strip() for x in (transport_legs or []) if str(x).strip()]
    stays = [str(x).strip() for x in (hotels or []) if str(x).strip()]
    if not legs and not stays:
        return ""
    out = ["### 已确认的交通与住宿\n"]
    if legs:
        out.append("\n- **交通**\n")
        out.extend(f"  - {t}\n" for t in legs)
    if stays:
        out.append("\n- **住宿**\n")
        out.extend(f"  - {h}\n" for h in stays)
    return "".join(out)

def _render_itinerary_body(
    days: list, budget: dict, req: dict,
    transport_legs: list | None = None, hotels: list | None = None,
) -> str:
    """结构化逐日行程 → Markdown 正文 + 预算明细（全部为代码生成，数字不来自模型）。
    transport_legs / hotels：状态机已确认并落库的交通段与住宿
    （selected_transport_legs / selected_hotels）。
    """
    if not days:
        return ""
    confirmed = _render_confirmed_block(transport_legs, hotels)
    lines = [confirmed + "\n", "### 详细行程\n"] if confirmed else ["### 详细行程\n"]

    for d in days:
        city = str(d.get("city") or "").strip()
        lines.append(f"\n#### Day {d.get('day')}{' ' + city if city else ''}\n")
        for key, label in _SLOT_RENDER:
            items = [str(x).strip() for x in (d.get(key) or []) if str(x).strip()]
            lines.append(f"\n- **{label}**\n")
            lines.extend(f"  - {it}\n" for it in items)
        # 当天小贴士
        tips = [str(x).strip() for x in (d.get("tips") or []) if str(x).strip()]
        if tips:
            lines.append("\n- **小贴士**：\n")
            lines.extend(f"  - {t}\n" for t in tips)

    b = budget or {}
    if b.get("total"):
        people = int(req.get("adult_count") or 0) + int(req.get("children_count") or 0) or 1
        total = float(b.get("total") or 0)
        per = total / people
        # ★ 与 _build_budget（自动确认模式）口径完全一致：表头人数/晚数/间数/城数 + 各分项括号说明
        day_count = int(req.get("travel_days") or 0) or len(days) or 1
        nights = max(day_count - 1, 1)
        rooms = max((people + 1) // 2, 1)      # ceil(people/2)，避免新增 math 依赖
        city_count = len({str(d.get("city")).strip() for d in days
                          if str(d.get("city") or "").strip()}) or 1
        transport_cost = float(b.get("transport") or 0)
        accommodation_cost = float(b.get("accommodation") or 0)
        food_cost = float(b.get("food") or 0)
        attractions_cost = float(b.get("attractions") or 0)
        misc_cost = float(b.get("misc") or 0)
        total_legs = len(transport_legs or [])
        transport_detail = (f"（{total_legs} 段 × 各段票价）" if total_legs
                            else "（本地游玩，无城际交通段）")
        hotel_price = (accommodation_cost / (nights * rooms)) if nights * rooms else 0.0
        hotel_detail = (f"各城均价 {hotel_price:.0f} 元" if hotels
                        else f"各城估算 {hotel_price:.0f} 元/晚（实时房价不可用）")
        bmax = req.get("budget_max")
        if isinstance(bmax, (int, float)) and bmax > 0:
            verdict = "人均未超预算" if per <= bmax else "人均超预算（建议下调住宿档位或减天数）"
        else:
            verdict = "未提供预算上限，仅作参考"
        # ★ 门票说明须与金额来源一致：来源字段缺席（如快速通道预算）时回退估算文案，
        #   否则模型传了真实票价时，金额按真实价算、说明却仍写"按 200元/人/天 估算"，数字与说明自相矛盾
        ticket_note = (f"（实时票价合计 {attractions_cost / people:.0f} 元/人）"
                       if b.get("attractions_source") == "real" else "（按 200元/人/天 估算）")
        lines.append(
            f"\n### 预算明细（{people} 人 / {nights} 晚 / {rooms} 间房 / {city_count} 城）\n"
            f"- **交通费用**：{transport_cost:.2f} 元"
            f"（人均 {transport_cost / people:.0f} 元，{transport_detail}）\n"
            f"- **住宿费用**：{accommodation_cost:.2f} 元"
            f"（{rooms} 间 × {nights} 晚 × {hotel_detail}）\n"
            f"- **餐饮费用**：{food_cost:.2f} 元（150元/人/天）\n"
            f"- **景点门票**：{attractions_cost:.2f} 元{ticket_note}\n"
            f"- **其他杂费**：{misc_cost:.2f} 元\n"
            f"**总计：{total:.2f} 元（{people} 人总价）｜人均：{per:.2f} 元**\n"
            f"预算结论：{verdict}\n"
        )
    else:
        app_logger.warning("⚠️ 行程渲染时缺少预算数据，本轮不输出预算明细（避免编造数字）")
    return "".join(lines).strip()

# 代码渲染正文分段推送，模拟流式观感
_RENDER_CHUNK = 48
_RENDER_DELAY = 0.025

async def _push_rendered(stream: EventStream, text: str) -> None:
    """把代码渲染的长正文按小段渐进推送（文本短于一段时零额外延迟）"""
    for i in range(0, len(text), _RENDER_CHUNK):
        stream.push(make_event(ROLE_ASSISTANT, text[i:i + _RENDER_CHUNK]))
        if i + _RENDER_CHUNK < len(text):
            await asyncio.sleep(_RENDER_DELAY)

def _queried_cities(tail: list) -> list[str]:
    """从本轮 query_destination_info 的调用参数里取城市
    多城"排序确认轮"selected_destination为空时，作为城市来源
    """
    out: list[str] = []
    for m in tail:
        for tc in (getattr(m, "tool_calls", None) or []):
            if (tc.get("name") if isinstance(tc, dict) else None) != "query_destination_info":
                continue
            city = str(((tc.get("args") or {}) if isinstance(tc, dict) else {}).get("destination") or "").strip()
            if city and city not in out:
                out.append(city)
    return out

async def _weather_paragraph(cities: list[str], start: str, days: int) -> str:
    """代码直查并段落式渲染行程窗口内天气。
    返回空字符串表示查询失败/无可用预报，绝不编造天气。
    """
    cities = [c.strip() for c in cities if c.strip()][:_WEATHER_CITY_MAX]
    if not cities:
        return ""
    from app.tools.mcp_tools import get_weather_tools
    from app.agents.routers.destination_router import WeatherForecast, _normalize_weather_payload

    tools = await get_weather_tools()
    tool = next((t for t in tools if t.name == "get_weather_forecast"), None)
    if tool is None:
        app_logger.warning("天气工具未加载，本轮不追加天气（不编造）")
        return ""
    try:
        begin = date.fromisoformat(start) if start else None
    except ValueError:
        begin = None
    end = begin + timedelta(days=days - 1) if (begin and days > 0) else None
    parts: list[str] = []
    for city in cities:
        try:
            payload = _normalize_weather_payload(await tool.ainvoke({"city": city}))
            if payload.get("error"):
                raise RuntimeError(str(payload["error"]))
            casts = WeatherForecast.model_validate(payload).casts
        except Exception as e:
            app_logger.warning(f"天气渲染跳过 {city}：{type(e).__name__}: {e}")
            continue
        if begin and end:
            in_window = [c for c in casts if begin <= date.fromisoformat(c.date) <= end]
            casts = in_window or casts[:max(days, 1)]
        else:
            casts = casts[:days] if days > 0 else casts
        if not casts:
            continue
        # 渲染天气文本块
        header = (
            f"出发日期前后（{begin.isoformat()}至{end.isoformat()}）的天气概况如下："
            if begin and end else f"未来 {len(casts)} 天天气概况如下："
        )
        rows = [
            f"- {c.date}: 白天{c.dayweather}，气温{c.nighttemp}~{c.daytemp}°C" for c in casts
        ]
        note = ""
        if begin and end:
            missing = days - len(casts)
            if missing == days:
                note = f"（{begin.isoformat()} 起超出可预报范围，暂提供未来 {len(casts)} 天，临近出发再查）"
            elif missing > 0:
                note = f"（后 {missing} 天超出可预报范围，临近出发再查）"
        block = "\n".join([header] + rows + ([note] if note else []))
        parts.append(f"### {city}\n{block}" if len(cities) > 1 else block)
    if not parts:
        return ""
    return "\n\n".join(parts) + "\n\n（数据来源：高德天气）"

# 住宿轮外部酒店服务不可用，代码层确定性兜底逻辑
# 触发条件：①工具返回降级标记；②酒店查询工具加载失败。兜底仅输出品牌档位，不编造门店与价格
_DEGRADE_MARK = "【外部服务暂时不可用】"
_ACCOMMODATION_STEP = "accommodation_planning"

# 交通规划步骤采用整块缓冲，回合结束一次性推送，避免大量班次逐字流式造成卡顿
_TRANSPORT_STEP = "transport_planning"

# 行程生成步骤改为代码确定性推进，减少一轮用户确认交互
_ITINERARY_STEP = "itinerary_generation"

# 需要整块缓冲的步骤：一次性推送结果，防止流式重置造成前端内容闪变
_BUFFER_ALL_STEPS = {
    _TRANSPORT_STEP,
    "destination_recommendation",
    _ITINERARY_STEP,
    "requirement_collection",
}
# 住宿、餐饮确认轮也加入缓冲集合，防止前端内容闪变
_BUFFER_ALL_STEPS |= {_ACCOMMODATION_STEP, "food_planning"}

def _covers_all_days(text: str, days: int) -> bool:
    """校验正文是否完整覆盖第1~days天，复用落库守卫的天数解析器，统一口径"""
    if days <= 0 or not text:
        return False
    from app.tools.state_transition import _day_numbers  # 局部导入，防止循环引用
    have = _day_numbers(text)
    return all(d in have for d in range(1, days + 1))

async def execute_turn(
        user_id: str, conversation_id: str, content: str, planning: bool, stream: EventStream
) -> tuple[str, str | None, bool, list | None]:
    """执行阶段：规划请求走 LangGraph Agent，闲聊直连 LLM
    返回 (完整回复文本, 状态机当前步骤, 本轮是否落库, 逐日结构化行程)：
    - current_step 供服务层做行程清单生成的步骤门禁
    - committed 表示本轮是否真正调用了 generate_itinerary_tool 落库
    - itinerary_body 为已过 Pydantic 校验的逐日结构，供清单生成兜底构造
    - 闲聊路径无状态机，后三项返回 None/False/None
    """
    if planning:
        agent = await create_travel_agent()
        input_data = {"messages": [HumanMessage(content=content)], "user_id": user_id}
        # LangGraph recursion_limit 上调，给多轮工具往返留余量
        config = {"configurable": {"thread_id": conversation_id}, "recursion_limit": 60}
        try:
            pre_snap = await agent.aget_state(config)
            turn_step = (pre_snap.values or {}).get("current_step")
            buffer_all = turn_step in _BUFFER_ALL_STEPS
        except Exception:
            buffer_all = False
            turn_step = None

        # 交通方式切换：用户对话内修改交通方式，预先落库更新状态
        _switch_note = ""
        if turn_step == _TRANSPORT_STEP:
            from app.services.transport_switch_service import apply_transport_switch
            _switched = await apply_transport_switch(agent, config, pre_snap.values or {}, content)
            if _switched:
                _switch_note = f"好的，已按您的要求把交通方式改为「{_switched}」。"

        text = await _stream_agent(
            agent, input_data, config, stream,
            buffer_all=buffer_all,
            defer_push=buffer_all,
        )

        # 回合结束读取最终状态快照
        try:
            snap = await agent.aget_state(config)
            state_step = (snap.values or {}).get("current_step")
        except Exception:
            app_logger.warning("读取状态机 current_step 失败，本轮禁止生成行程清单")
            state_step = None

        # 判断本轮是否执行行程落库
        committed = False
        tail: list = []
        try:
            msgs = (snap.values or {}).get("messages") or []
            for m in reversed(msgs):
                if getattr(m, "type", None) == "human":
                    break
                tail.append(m)
            committed = any(
                getattr(m, "type", None) == "tool" and "已生成" in str(getattr(m, "content", ""))
                for m in tail
            )
        except Exception:
            committed = False

        # 获取结构化行程 body
        try:
            body = (snap.values or {}).get("itinerary_body") or None
        except Exception:
            body = None

        # 后处理①：行程步骤自动推进，去除用户确认轮
        if state_step == _ITINERARY_STEP and not committed:
            _days = int(((snap.values or {}).get("user_requirement") or {}).get("travel_days") or 0)
            if _covers_all_days(text, _days):
                try:
                    await agent.aupdate_state(config, {
                        "itinerary": [{"day_number": d} for d in range(1, _days + 1)],
                        "current_step": "budget_summarization",
                    })
                    state_step = "budget_summarization"
                    app_logger.info(
                        f"✅ 行程正文已覆盖第1~{_days}天，代码层推进到预算步（去掉确认轮）"
                    )
                except Exception:
                    app_logger.warning("正文达标但状态推进失败，本轮按原步骤处理", exc_info=True)

        # 后处理②：同城本地游玩，零城际段，直接跳到住宿步骤
        _zero_leg, _dep0 = _zero_leg_guard(snap.values or {})
        _zero_note = ""
        if state_step == _TRANSPORT_STEP and _zero_leg:
            from app.capabilities.train_multi_segment.train_flow import advance_zero_leg
            if await advance_zero_leg(conversation_id):
                _zero_note = (f"出发地与目的地同为{_dep0}，本次为本地游玩，无需城际交通段，"
                              "直接为您安排住宿。")
                text = _zero_note
                state_step = _ACCOMMODATION_STEP

        # 后处理③：住宿服务降级兜底
        if state_step == _ACCOMMODATION_STEP:
            degraded = any(
                getattr(m, "type", None) == "tool" and _DEGRADE_MARK in str(getattr(m, "content", ""))
                for m in tail
            )
            if degraded or not await _has_hotel_search_tool():
                _vals = snap.values or {}
                _last_human = ""
                for _m in reversed(_vals.get("messages") or []):
                    if getattr(_m, "type", None) == "human":
                        _last_human = str(getattr(_m, "content", "") or "")
                        break
                _chosen = _match_hotel_choice(_last_human, _vals.get("user_requirement"))
                if _chosen:
                    from app.tools.state_transition import _multi_city_names
                    _cities = _multi_city_names(_vals)
                    _rec = list(_vals.get("selected_hotels") or [])
                    for _c in (_cities or []):
                        if not any(_c in _r for _r in _rec):
                            _rec.append(f"{_c}：{_chosen}（具体门店到店选定）")
                    _upd: dict = {
                        "selected_hotels": _rec,
                        "current_step": "food_planning",
                    }
                    _types = list((_vals.get("user_requirement") or {}).get("accommodation_types") or [])
                    if _types:
                        _upd["selected_accommodation_types"] = _types
                    try:
                        await agent.aupdate_state(config, _upd)
                        state_step = "food_planning"
                        app_logger.warning(
                            f"✅ 住宿轮降级且用户已选「{_chosen}」：代码层直接推进到餐饮步"
                        )
                    except Exception:
                        app_logger.warning("住宿轮降级推进失败，本轮仍按住宿步处理", exc_info=True)
                recommendation = _build_hotel_fallback(
                    _vals.get("user_requirement"),
                    bool(_vals.get("auto_confirm")),
                    _chosen,
                )
                if not buffer_all:
                    stream.push(make_event(ROLE_ASSISTANT, recommendation))
                text += recommendation
                app_logger.warning(
                    f"⚠️ 住宿轮{'酒店服务降级' if degraded else '无酒店查询工具'}："
                    f"已补推常见连锁酒店品牌推荐（防止会话卡死）"
                )

        # 后处理④：多段住宿卡片逻辑
        if state_step == _ACCOMMODATION_STEP:
            from app.capabilities.hotel_multi_segment.hotel_flow import (
                auto_select_hotels,
                build_lead_text,
                emit_hotel_skill_ui,
                hotel_card_mode,
            )
            _hv = snap.values or {}
            if hotel_card_mode(_hv):
                if _hv.get("auto_confirm"):
                    _hp = await auto_select_hotels(conversation_id, _hv)
                    if _hp:
                        text = ("已按您表单中的住宿档位为您选定各站住宿：\n"
                                + "\n".join(f"- {t}" for t in _hp))
                else:
                    _hold, _hst = await emit_hotel_skill_ui(stream, conversation_id, _hv)
                    if _hst == "pushed":
                        text = build_lead_text(_hold)
                    elif _hst == "empty" and buffer_all:
                        text = ("⚠️ 各站住宿实时候选暂时查询不到（酒店数据源未返回结果），"
                                "请稍后在对话中重新查询。")
            if _zero_note and _zero_note not in text:
                text = f"{_zero_note}\n\n{text}"

        # 后处理⑤：目的地天气交由代码渲染，剔除模型生成天气文本
        if state_step == "destination_recommendation":
            _vals = snap.values or {}
            _req = _vals.get("user_requirement") or {}
            from app.services.express_planning_service import _split_cities
            _cities = _split_cities(
                str(_vals.get("selected_destination") or _req.get("destination") or "")
            )
            if not _cities:
                _cities = _queried_cities(tail)
            stripped = _strip_model_weather(text)
            if stripped != text:
                app_logger.warning("⚠️ 已剔除模型自写天气行/占位语（改由代码渲染）")
            text = stripped
            if turn_step == "destination_recommendation":
                app_logger.info(
                    f"⏭️ 目的地步非进入轮：跳过天气块追加（{_cities} 已在进入轮渲染过）"
                )
            else:
                block = await _weather_paragraph(
                    _cities, str(_req.get("departure_date") or ""), int(_req.get("travel_days") or 0)
                )
                if block:
                    _tail_q = ""
                    _lines = text.rstrip().splitlines()
                    if _lines and ("是否确认选择" in _lines[-1] or _lines[-1].strip().startswith("请问")):
                        _tail_q = _lines.pop()
                        text = "\n".join(_lines).rstrip()
                    text = f"{text}\n\n{block}" if text else block
                    if _tail_q:
                        text = f"{text}\n\n{_tail_q}"
                    app_logger.info(
                        f"✅ 天气已由代码渲染（{_cities} / 行程 {_req.get('travel_days')} 天，版式：引导句+每天一行）"
                    )
                    if not buffer_all:
                        await _push_rendered(stream, f"\n\n{block}")
                else:
                    app_logger.warning(f"⚠️ 代码渲染天气为空（城市={_cities}，不追加、不编造）")

        # 后处理⑥：交通清单代码接管，高铁/航班/自驾卡片
        if state_step == _TRANSPORT_STEP:
            leg = _leg_context(snap.values or {})[0]
            _vals = snap.values or {}
            from app.capabilities.train_multi_segment.train_flow import multi_leg_train
            from app.capabilities.flight_multi_segment.flight_flow import flight_card_mode
            from app.capabilities.driving_multi_segment.driving_flow import driving_card_mode
            _card_mode = bool(
                multi_leg_train(_vals) or flight_card_mode(_vals) or driving_card_mode(_vals)
            )
            pool = list(tail)
            try:
                pool += [
                    m for m in (snap.values or {}).get("messages") or []
                    if getattr(m, "type", None) == "tool"
                ]
            except Exception:
                pass
            table = _ticket_table_from_tail(pool, only_leg=leg)
            from app.tools.mcp_tools import parse_compact_rows, parse_ticket_rows
            fresh = any(
                getattr(m, "type", None) == "tool"
                and bool(parse_compact_rows(getattr(m, "content", "") or "")
                         or parse_ticket_rows(getattr(m, "content", "") or ""))
                for m in tail
            )
            if table and fresh and not _card_mode:
                cleaned = _strip_transport_lists(text)
                if cleaned != text:
                    if len(cleaned) < _TRANSPORT_LEAD_MIN:
                        cleaned = _TRANSPORT_LEAD
                    text = cleaned + table
                    app_logger.warning(
                        f"⚠️ 交通步骤正文含模型自编清单，已剔除并改用代码直出（{len(text)} 字）"
                    )
                else:
                    text += table
                app_logger.info("✅ 交通步骤已追加代码层权威班次清单（清单内为唯一有效车次）")
            elif table and _card_mode:
                app_logger.info(
                    "🚄 卡片模式：不直出 12306 班次表（班次已在选择卡片内，避免两套数据重复）"
                )

            # 航班不可达城市自动降级为高铁
            _fallback_note = ""
            from app.capabilities.flight_multi_segment.flight_flow import (
                flight_card_mode as _flight_card_mode,
                flight_unreachable_cities,
            )
            _no_air = flight_unreachable_cities(_vals) if _flight_card_mode(_vals) else []
            if _no_air:
                from app.services.transport_switch_service import switch_transport_mode
                try:
                    _vals = await switch_transport_mode(agent, config, _vals, "train")
                    _fallback_note = (
                        f"「{'、'.join(_no_air)}」暂无可直达的航班数据（该城市无民用机场或机场码暂未收录），"
                        f"已自动为您改查高铁；如仍希望乘机，可回复「换个目的地」重新挑选。"
                    )
                    app_logger.warning(
                        f"✈️→🚄 航班结构性不可查（无机场码城市：{_no_air}），已降级为高铁并交由高铁分支接管"
                    )
                except Exception as exc:
                    app_logger.warning(f"⚠️ 航班降级高铁落库失败（本轮按航班分支继续）：{exc}")

            # 多段高铁卡片
            if multi_leg_train(_vals):
                from app.capabilities.train_multi_segment.train_flow import (
                    emit_train_skill_ui,
                    auto_select_train_legs,
                    build_lead_text as build_train_lead_text
                )
                if _vals.get("auto_confirm"):
                    _picked = await auto_select_train_legs(conversation_id, _vals)
                    if _picked and buffer_all:
                        text = ("已按「耗时 + 价格」综合最优为您选定各段高铁：\n"
                                + "\n".join(f"- {t}" for t in _picked))
                        state_step = _ACCOMMODATION_STEP
                else:
                    _payload, _st = await emit_train_skill_ui(stream, conversation_id, _vals)
                    if _st == "pushed" and buffer_all:
                        text = build_train_lead_text(_payload)
                    elif _st == "empty" and buffer_all:
                        text = ("⚠️ 实时高铁班次暂时查询不到（12306 数据源本次未返回余票）。"
                                "您可以稍后发送「重新查询」，或直接回复「改航班」/「改自驾」，"
                                "我会立即按新的交通方式为您查询。")
            elif flight_card_mode(_vals):
                # 多段航班卡片
                from app.capabilities.flight_multi_segment.flight_flow import (
                    auto_select_flights,
                    build_lead_text as build_flight_lead_text,
                    emit_flight_skill_ui,
                )
                if _vals.get("auto_confirm"):
                    _fpicked = await auto_select_flights(conversation_id, _vals)
                    if _fpicked and buffer_all:
                        text = ("已按「价格最优」为您选定各段航班：\n"
                                + "\n".join(f"- {t}" for t in _fpicked))
                        state_step = _ACCOMMODATION_STEP
                else:
                    _fpayload, _fst = await emit_flight_skill_ui(stream, conversation_id, _vals)
                    if _fst == "pushed" and buffer_all:
                        text = build_flight_lead_text(_fpayload)
                    elif _fst == "empty" and buffer_all:
                        text = ("⚠️ 实时航班暂时查询不到（航班数据源本次未返回结果）。"
                                "您可以稍后发送「重新查询」，或直接回复「改高铁」/「改自驾」，"
                                "我会立即按新的交通方式为您查询。")
            elif driving_card_mode(_vals):
                # 多段自驾卡片
                from app.capabilities.driving_multi_segment.driving_flow import (
                    auto_select_driving,
                    build_lead_text as build_driving_lead_text,
                    emit_driving_skill_ui,
                )
                if _vals.get("auto_confirm"):
                    _dpicked = await auto_select_driving(conversation_id, _vals)
                    if _dpicked and buffer_all:
                        text = ("已按高德默认策略为您确认各段自驾路线：\n"
                                + "\n".join(f"- {t}" for t in _dpicked))
                        state_step = _ACCOMMODATION_STEP
                else:
                    _dpayload, _dst = await emit_driving_skill_ui(stream, conversation_id, _vals)
                    if _dst == "pushed" and buffer_all:
                        text = build_driving_lead_text(_dpayload)
                    elif _dst == "empty" and buffer_all:
                        text = ("⚠️ 实时自驾路线暂时查询不到（高德数据源本次未返回结果）。"
                                "您可以稍后发送「重新查询」，或直接回复「改高铁」/「改自驾」，"
                                "我会立即按新的交通方式为您查询。")
            # 追加交通切换/降级提示
            if buffer_all:
                for _note in (_fallback_note, _switch_note):
                    if _note and _note not in text:
                        text = f"{_note}\n\n{text}" if text.strip() else _note

        # 后处理⑦：结构化行程由代码渲染正文，追加预算与邀请话术
        code_rendered = False
        if any(
            getattr(m, "type", None) == "tool" and _BODY_READY_MARK in str(getattr(m, "content", ""))
            for m in tail
        ):
            try:
                vals = snap.values or {}
            except Exception:
                vals = {}
            rendered = _render_itinerary_body(
                vals.get("itinerary_body") or [],
                vals.get("budget") or {},
                vals.get("user_requirement") or {},
                vals.get("selected_transport_legs") or [],
                vals.get("selected_hotels") or [],
            )
            if rendered:
                rendered_text = rendered + _INVITE_TAIL
                prefix = text.strip()
                _req = vals.get("user_requirement") or {}
                _lead = (f"已按 {int(_req.get('travel_days') or 0)} 天行程生成如下安排："
                         if _req.get("travel_days") else "")
                text = f"{_lead}\n\n{rendered_text}" if _lead else rendered_text
                if prefix:
                    app_logger.warning(f"⚠️ 行程正文分支丢弃模型旁白 {len(prefix)} 字（正文仅由代码渲染）")
                state_step = "budget_summarization"
                code_rendered = buffer_all
                if not buffer_all:
                    stream.push(make_event(ROLE_ASSISTANT_RESET))   
                    await _push_rendered(stream, f"\n\n{text}")
                app_logger.info(
                    f"✅ 行程正文已由代码渲染（{len(rendered_text)} 字），引导语由代码生成"
                    f"（共 {len(text)} 字）"
                )

        # 缓冲模式：回合结束一次性推送最终文本
        if buffer_all and text.strip():
            if code_rendered:
                await _push_rendered(stream, text)
            else:
                stream.push(make_event(ROLE_ASSISTANT, text))
        return text, state_step, committed, body

    # 闲聊分支：天气/交通查询意图都走确定性兜底，不交给裸模型
    _intent = detect_intent(content)
    if _intent.weather_object:
        _wx = await try_weather_query(content, stream, intent=_intent)
        app_logger.info(f"[weather] 闲聊通道改推代码直查天气：{content[:30]}")
        return _wx, None, False, None
    if _intent.transport_object:
        _wx = await try_direct_ticket_query(content, stream)
        if _wx:
            app_logger.info(f"[fallback] 闲聊通道改推确定性直查结果：{content[:30]}")
            return _wx, None, False, None
        _reply = _query_fallback_text(content, _intent)
        stream.push(make_event(ROLE_ASSISTANT, _reply))
        return _reply, None, False, None
    return await _stream_plain(content, stream), None, False, None