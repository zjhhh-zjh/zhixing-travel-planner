"""
住宿候选解析核心（酒店 MCP 原始返回 → 标准门店 / 品牌候选）

来源：原 app/capabilities/hotel_single/hotel_agent_tool.py 的解析部分（单段 Skill 已下线，整体搬迁至此），
全项目只保留这一份解析口径，供 hotel_multi_segment 按段并行调用。

解析策略（两级兜底，因 MCP 原始返回格式可能随服务端版本变化）：
  ① JSON 优先：正文即 JSON → json.loads；
  ② 结构化取值：递归找"最像酒店记录"的字典列表，按多候选键名取值（中英文键都认）；
  两级都拿不到门店 → 由调用方降级 brand 模式（本模块只负责如实返回空列表）。
"""
import json
import re
from datetime import date as _date
from typing import Any, Optional

from langchain_core.tools import BaseTool

from app.capabilities.hotel_multi_segment.hotel_skill_schema import HotelBrand, HotelOption
from app.tools.mcp_tools import get_all_mcp_tools
from app.utils.logger import app_logger

# ==================== 品牌兜底口径 ====================
# 与 app/services/chat_stream_executor.py 的 _COMMON_HOTELS 同口径（国民级认知度品牌，
# 不含具体门店与实时价格，避免与实时报价混淆）；此处本地常量避免 Skill 反向依赖大服务模块
_BRANDS: dict[str, list[str]] = {
    "economy_hotel": ["如家", "汉庭", "7 天", "锦江之星", "格林豪泰", "莫泰"],
    "star_hotel": ["希尔顿", "万豪", "洲际", "香格里拉", "凯悦", "雅高"],
    "hostel": ["花间堂", "途家民宿", "小猪民宿", "美团民宿"],
    "youth_hostel": ["国际青年旅舍（YHA）", "万里路青年旅舍", "如家青旅"],
}
# priceLevel 文案 → 档位键（未命中一律按经济型，与项目住宿档位口径一致）
_LEVEL_HINTS = (
    ("youth_hostel", ("青年", "青旅", "YHA")),
    ("hostel", ("民宿", "客栈", "公寓")),
    ("star_hotel", ("星级", "高端", "豪华", "五星", "四星", "舒适")),
    ("economy_hotel", ("经济", "快捷", "低", "预算")),
)
# 档位 → MCP starRatings 入参（按项目既有 prompt 口径：高预算 [4.5,5.0]，低预算 [0.0,3.0]）
_STAR_RATINGS = {
    "economy_hotel": [0.0, 3.0],
    "star_hotel": [4.5, 5.0],
    "hostel": [0.0, 5.0],
    "youth_hostel": [0.0, 5.0],
}

# 酒店查询工具：按名过滤（与 app/tools/mcp_tools.get_hotel_tools 同口径）
_HOTEL_TOOL_HINTS = ("searchhotels", "find-hotels")

# ==================== 记录键名候选（中英文都认，服务端换字段名时不至于解析不出） ====================
_NAME_KEYS = ("hotelname", "hotel_name", "hotel", "name", "title", "酒店名称", "店名", "名称")
_TOTAL_KEYS = ("totalprice", "total_price", "totalamount", "total_amount", "total", "总价", "合计", "总计")
_NIGHT_KEYS = (
    "pricepernight", "price_per_night", "nightlyprice", "nightly_price",
    "avgprice", "avg_price", "unitprice", "price", "每晚", "单价", "均价",
)
_RATING_KEYS = ("starrating", "star_rating", "star", "rating", "score", "评分", "星级", "钻级")
_ADDR_KEYS = ("address", "addr", "location", "position", "地址", "位置", "详细地址")
# 价格在 aigohotel 真实返回里是**嵌套对象**（实测）：
#   "price": {"message":"查价成功。1晚总价：193CNY（约193CNY/晚）","hasPrice":true,"lowestPrice":193.0}
# 故优先取 price.* 子字段
_PRICE_OBJ_KEYS = ("price", "priceinfo", "pricing")
_PRICE_NIGHT_KEYS = ("lowestprice", "pricepernight", "nightlyprice", "pernightprice", "avgprice")
_PRICE_TOTAL_KEYS = ("totalprice", "totalamount", "total")

# 查询工具缓存（一次解析全量工具列表后复用）
_QUERY_TOOL: Optional[BaseTool] = None


# ==================== 档位 / 日期 / 数值小工具 ====================

def _level_of(price_level: str) -> str:
    """档位文案 → 档位键（未命中按经济型）"""
    text = price_level or ""
    for level, hints in _LEVEL_HINTS:
        if any(hint in text for hint in hints):
            return level
    return "economy_hotel"


def stay_nights(check_in: str, check_out: str) -> int:
    """住几晚 = 离店 - 入住；日期不合法或倒挂时按 1 晚兜底（不做业务校验）"""
    try:
        span = (_date.fromisoformat(str(check_out)[:10]) - _date.fromisoformat(str(check_in)[:10])).days
        return span if span > 0 else 1
    except ValueError:
        return 1


def _to_number(value: Any) -> float:
    """从任意形态取值里抠出数字（'¥302' / '302元' / '4.5分' / 302 → 302.0）；取不到返回 0"""
    if isinstance(value, (int, float)):
        return float(value)
    hit = re.search(r"\d+(?:\.\d+)?", str(value or ""))
    return float(hit.group()) if hit else 0.0


# ==================== 第 ① 级：JSON 解析 ====================

def _raw_text(raw: Any) -> str:
    """MCP 返回可能是 content blocks 列表，统一取纯文本"""
    if isinstance(raw, list):
        return "\n".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in raw
        )
    return str(raw or "")


def _load_structured(text: str) -> Any:
    """把返回文本解析成 Python 对象：整体 json"""
    try:
        return json.loads((text or "").strip())
    except (ValueError, TypeError):
        return None


# ==================== 第 ② 级：结构化取值（按候选项键名归一） ====================

def _pick(record: dict, keys: tuple[str, ...]) -> Any:
    """按候选键名取值（键名大小写/下划线不敏感）；取不到返回 None"""
    # 键名归一：去下划线 + 去空白 + 转小写（纯字符串处理，等价于原 re.sub(r"[_\s]", "", k)）
    flat = {"".join(str(k).replace("_", "").split()).lower(): v for k, v in record.items()}
    for key in keys:
        value = flat.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _as_text(value: Any) -> str:
    """任意取值转文本"""
    return str(value).strip()


def _numeric(record: dict, keys: tuple[str, ...]) -> float:
    """按候选键取**标量**数值（跳过 dict/list，避免把嵌套 price 对象误当价格）"""
    # 键名归一：去下划线 + 去空白 + 转小写（与 _pick 同款纯字符串处理）
    flat = {"".join(str(k).replace("_", "").split()).lower(): v for k, v in record.items()}
    for key in keys:
        value = flat.get(key)
        if isinstance(value, (int, float, str)) and _to_number(value):
            return _to_number(value)
    return 0.0


def _price_pair(record: dict, nights: int) -> tuple[float, float]:
    """取（总价, 每晚单价）：嵌套 price 对象 → 扁平键，缺一方按晚数互补

    实测 aigohotel 的 price 是对象且含 lowestPrice（每晚价），故：
    lowestPrice 当每晚价；总价不返回时按 每晚价 × 晚数 换算（纯单位换算，不做业务校验）。
    """
    total = night = 0.0
    nested = next((v for v in (_pick(record, (k,)) for k in _PRICE_OBJ_KEYS) if isinstance(v, dict)), None)
    if nested:
        night = _numeric(nested, _PRICE_NIGHT_KEYS)
        total = _numeric(nested, _PRICE_TOTAL_KEYS)
    total, night = total or _numeric(record, _TOTAL_KEYS), night or _numeric(record, _NIGHT_KEYS)
    if not total and night:
        total = night * nights
    elif total and not night:
        night = total / nights
    return total, night


def _record_score(record: dict) -> int:
    """判断一个 dict 有多像"酒店记录"：命中名称 + 价格 + 评分 + 地址的键越多越像"""
    return sum(1 for keys in (_NAME_KEYS, _NIGHT_KEYS, _RATING_KEYS, _ADDR_KEYS)
               if _pick(record, keys) not in (None, "", [], {}))


def _best_record_list(node: Any, depth: int = 0) -> list[dict]:
    """递归找"最像酒店列表"的字典数组（按平均像度打分取最高，避免误选城市/分页等数组）"""
    best: list[dict] = []
    best_score = 0
    if depth > 6:
        return best
    if isinstance(node, dict):
        for value in node.values():
            found = _best_record_list(value, depth + 1)
            score = sum(_record_score(item) for item in found) / len(found) if found else 0
            if found and score > best_score:
                best, best_score = found, score
    elif isinstance(node, list):
        dicts = [item for item in node if isinstance(item, dict)]
        if dicts:
            avg = sum(_record_score(item) for item in dicts) / len(dicts)
            # ★ 平均命中名称 / 价格 / 评分 / 地址中的两类以上，才算"酒店列表"
            if avg >= 2:                          # 至少像"名称 + 一项数值"，才算候选列表
                return dicts
        for item in node:
            found = _best_record_list(item, depth + 1)
            score = sum(_record_score(r) for r in found) / len(found) if found else 0
            if found and score > best_score:
                best, best_score = found, score
    return best


# ==================== 原始行 → HotelOption（统一定价口径） ====================

def _normalize_hotel(record: dict, index: int, nights: int) -> Optional[HotelOption]:
    """统一记录 → HotelOption：门店名必须有；价格按 _price_pair 归一（不做业务校验）"""
    name = _as_text(_pick(record, _NAME_KEYS))
    if not name:
        return None
    total, night = _price_pair(record, nights)
    return HotelOption(
        hotel_id=f"h{index}",
        hotelName=name,
        totalPrice=total,
        pricePerNight=night,
        starRating=_to_number(_pick(record, _RATING_KEYS)),
        address=_as_text(_pick(record, _ADDR_KEYS)),
    )


def hotels_from_raw(raw: Any, nights: int, limit: int) -> list[HotelOption]:
    """原始返回 → 门店候选（两级解析；解析不出返回空列表，由调用方降级 brand 模式）"""
    text = _raw_text(raw)
    structured = _load_structured(text)
    records = _best_record_list(structured) if structured is not None else []
    options = [opt for i, rec in enumerate(records, 1)
               if (opt := _normalize_hotel(rec, i, nights)) is not None]
    app_logger.info(
        f"🏨 酒店解析：原始 {len(text)} 字 → 门店候选 {len(options)} 个（取前 {limit} 个）"
    )
    return options[:limit]


def brand_fallback(price_level: str) -> list[HotelBrand]:
    """实时报价不可用时的品牌兜底：按档位给出常见连锁品牌（不编造门店与价格）"""
    level = _level_of(price_level)
    return [HotelBrand(brand_id=f"b{i}", brandName=name)
            for i, name in enumerate(_BRANDS[level], 1)]


# ==================== 查询工具定位与入参适配 ====================

async def resolve_hotel_tool() -> Optional[BaseTool]:
    """定位酒店查询工具：按名含 searchhotels / find-hotels 判定（与 get_hotel_tools 同口径）

    注意取 get_all_mcp_tools() 的**原始工具**：本 Skill 需要解析原始返回文本的完整结构，
    不用被裁剪/包装过的工具版本。
    """
    global _QUERY_TOOL
    if _QUERY_TOOL is not None:
        return _QUERY_TOOL
    for item in await get_all_mcp_tools():
        name = getattr(item, "name", "").lower()
        if any(hint in name for hint in _HOTEL_TOOL_HINTS):
            _QUERY_TOOL = item
            app_logger.info(f" 复用酒店查询工具：{item.name}")
            return _QUERY_TOOL
    app_logger.warning("⚠️ 未找到酒店查询工具（searchHotels / find-hotels），将走品牌兜底")
    return None


def build_args(
    tool: BaseTool, city: str, check_in_date: str, nights: int,
    guest_count: int, price_level: str, max_options: int,
) -> dict:
    """组装酒店工具入参：aigohotel searchHotels 的嵌套形态

    实测 aigohotel 的 searchHotels：必填 originQuery / place / placeType（取值「城市」等中文），
    日期-晚数-人数在 checkInParam 里，星级筛选在 filterOptions 里，条数在 size 里。
    """
    stars = _STAR_RATINGS[_level_of(price_level)]
    return {
        "originQuery": f"{city} {price_level}酒店 {nights}晚 {guest_count}人",
        "place": city, "placeType": "城市",
        "checkInParam": {
            "checkInDate": check_in_date, "stayNights": nights, "adultCount": guest_count,
        },
        "filterOptions": {"starRatings": stars},
        "size": max_options,
    }


__all__ = [
    "hotels_from_raw",
    "brand_fallback",
    "resolve_hotel_tool",
    "build_args",
    "stay_nights",
]