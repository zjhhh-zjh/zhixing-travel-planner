"""行程清单生成服务：DeepSeek 结构化提炼"""
import asyncio
import re
import uuid
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession  # 类型注解仍被 get_plan/generate_plan 使用
from pydantic import BaseModel, Field
from sqlalchemy.orm import Mapped

from app.config import settings
from app.models.itinerary_plan import ItineraryPlan
from app.models.conversation import Conversation  # 归属校验用（join 会话表）
from app.utils.llm_factory import get_deepseek_llm
from app.utils.logger import app_logger
from app.services.quality_check_service import schedule_quality_check  # 在线质检


async def get_plan(db: AsyncSession, plan_id: str, conversation_id: uuid.UUID) -> ItineraryPlan | None:
    """REST 查询：按 planId + 会话查行程清单"""
    result = await db.execute(
        select(ItineraryPlan).where(
            ItineraryPlan.plan_id == plan_id,
            ItineraryPlan.conversation_id == conversation_id,
        )
    )
    return result.scalar_one_or_none()


async def get_owned_plan(db: AsyncSession, plan_id: str, user_id: uuid.UUID) -> ItineraryPlan | None:
    """REST 查询：按 planId 查清单并校验归属

    资源式路径 /api/v1/itinerary-plans/{plan_id} 不再传 sessionId，故改为
    join conversation 表校验 user_id——单次查询同时完成"查到 + 防横向越权"。
    """
    result = await db.execute(
        select(ItineraryPlan)
        # ★ 清单表没有 user_id：顺着"清单 → 会话 → 用户"join，一条 SQL 同时"查到 + 防越权"
        .join(Conversation, Conversation.id == ItineraryPlan.conversation_id)
        .where(ItineraryPlan.plan_id == plan_id, Conversation.user_id == user_id)
    )
    return result.scalar_one_or_none()

# ============== 前端 TravelItinerary 契约结构（字段名与网页端严格一致） ==============

class ScenicSpot(BaseModel):
    """景点"""
    toponym: str = Field(description="景点名称")
    toponym_desc: str = Field(description="景点介绍，50-100字")
    toponym_image: str = Field(description="封面图 URL，无则空字符串")
    toponym_image_list: list[str] = Field(description="图片列表，无则空数组")
    longitude: float | None = Field(default=None, description="经度")
    latitude: float | None = Field(default=None, description="纬度")


class ItineraryItem(BaseModel):
    """时段安排"""
    time: str = Field(description="时段，如：上午/下午/晚上")
    scenic_spots_list: list[ScenicSpot]


class ItineraryDay(BaseModel):
    """一天"""
    id: str = Field(description="唯一标识，随机字符串")
    day: str = Field(description="第几天，如：第一天")
    itinerary: list[ItineraryItem]
    route_polylines: list[list[float]] = Field(default_factory=list)  # 每段驾车路线平铺坐标 [lat,lng,...]（高德方向API）


# ★ 四层嵌套对着前端组件层级；字段名与前端 TSX 逐字一致，改一个前端就崩
class TravelItinerary(BaseModel):
    """行程清单（前端渲染契约）"""
    title: str = Field(description="行程标题")
    itinerary_list: list[ItineraryDay]

# 清单"必定生成"的代码兜底。LLM 提炼环节有多种失败可能（DeepSeek 欠费/
# 思考模式偶发不返回 tool_calls/返回体不合 schema），任一失败都会让用户拿不到清单——但此时
# 状态机里的 itinerary_body 已是 Pydantic 校验过的结构，信息完整，可直接由代码构造清单。
# 故提炼只作为"增强手段"，不再是单点依赖。
_SLOT_TIME_LABELS = (
    ("morning", "上午"), ("noon", "中午"), ("afternoon", "下午"), ("evening", "晚上"),
)


_LEAD_TIME_RE = re.compile(r"^\s*\d{1,2}[:：]\d{2}(\s*[-~—]\s*\d{1,2}[:：]\d{2})?\s*(前后|左右)?\s*")


def _spot_from_text(text: str) -> ScenicSpot:
    """兜底清单的景点条目：条目首段作为景点名，整条作为介绍（图片/坐标留空，由增强补齐）

    ★ 必须先剥掉开头时刻：按冒号切分会把 "09:00 天安门广场观升旗" 切成 "09"，
      随后高德 POI 拿 "09" 去搜坐标与封面，整份兜底清单既没坐标也没图
    """
    text = text.strip()
    head = re.split(r"[，,。；;：:\s（(]", _LEAD_TIME_RE.sub("", text), maxsplit=1)[0][:24]
    return ScenicSpot(
        toponym=head or text[:24], toponym_desc=text,
        toponym_image="", toponym_image_list=[],
    )

def _fallback_title(markdown: str, days: int) -> str:
    """兜底标题：优先取源料首个标题行，否则用"N 天行程规划" """
    for line in (markdown or "").splitlines():
        head = line.strip().lstrip("#＊* ").strip()
        if head:
            return head[:30]
    return f"{days} 天行程规划"


def _plan_from_structure(days: list[dict], markdown: str = "") -> TravelItinerary:
    """用已过校验的 itinerary_body 直接构造行程清单（不依赖任何 LLM 提炼）"""
    out: list[ItineraryDay] = []
    for d in days or []:
        if not isinstance(d, dict):
            continue
        items = [
            ItineraryItem(
                time=label,
                scenic_spots_list=[_spot_from_text(str(t)) for t in (d.get(key) or []) if str(t).strip()],
            )
            for key, label in _SLOT_TIME_LABELS if d.get(key)
        ]
        out.append(ItineraryDay(
            id=uuid.uuid4().hex[:12],
            day=f"第{d.get('day')}天",
            itinerary=items,
        ))
    # ★ 全程不经 LLM：用已过 Pydantic 校验的 itinerary_body 直接拼出一份合法清单
    return TravelItinerary(title=_fallback_title(markdown, len(out)), itinerary_list=out)


_JSON_PROMPT = (
    "从下面的行程规划内容中提取结构化行程。要求：\n"
    "1. 严格按工具的 schema 传参，字段名不得改动\n"
    "2. 景点经纬度从原文提取，缺失填 null\n"
    "3. 图片 URL 从原文提取，缺失用空字符串/空数组\n"
    "4. toponym_desc 为 50-100 字景点介绍：原文有描述则提炼总结；"
    "原文只有一句话时，根据该景点的公开常识补全介绍（地理位置/特色/游玩方式），"
    "确保清单信息量充足——但门票价格/营业数据等事实数字严禁编造，原文没有就留空相关表述\n"
    "5. 必须通过工具调用提交结果\n\n"
    "行程内容：\n"
)


def _clean_keyword(name: str) -> str:
    """ 清洗模型提取的景点名：去序号前缀（"1."/"2、"）与括号注解（"（返程）"），
    提升高德 POI 关键词命中率（原名如"1.杭州东站（返程）"会直接搜空）"""
    name = re.sub(r"^\s*\d+\s*[.、．:：]\s*", "", name)
    name = re.sub(r"[（(][^（）()]*[)）]", "", name)
    return name.strip()

async def _enrich_spots(plan: TravelItinerary) -> None:
    """高德 POI 补全坐标与图片

    DeepSeek 只能从 RAG 文本提取，图片/经纬度经常缺失；用高德地点搜索补全。
    高德返回 GCJ-02 坐标，与腾讯地图渲染直接兼容，无需转换。
 两段式关键词（清洗名→原始名）+ 静态地图兜底，尽量保证坐标与封面不缺失
    """
    url = "https://restapi.amap.com/v3/place/text"
    async with httpx.AsyncClient(timeout=8) as client:
        for day in plan.itinerary_list:
            for item in day.itinerary:
                for spot in item.scenic_spots_list:
                    if spot.longitude is not None and spot.toponym_image:
                        continue
                    cleaned = _clean_keyword(spot.toponym)
                    poi = None
                    # 两段式搜索：先清洗名（高），再退回原始名；任一命中即用
                    for kw in dict.fromkeys([cleaned, spot.toponym]):
                        if not kw:
                            continue
                        try:
                            res = await client.get(url, params={
                                "keywords": kw,
                                "key": settings.amap_api_key,
                                "offset": 1,
                            })
                            data = res.json()
                            pois = data.get("pois") or []
                            if not pois:
                                # 区分"真无结果"与"限流拒答"：高德个人 key QPS 有限，
                                # 超速请求 status!=1 被静默拒答，打日志便于诊断
                                if data.get("status") != "1":
                                    app_logger.warning(
                                        f"高德POI限流/异常: {kw} -> {data.get('infocode')} {data.get('info')}"
                                    )
                                await asyncio.sleep(0.25)  # 限流疑虑时也控速后再试下一关键词
                                continue
                            poi = pois[0]
                            break
                        except Exception:
                            app_logger.warning(f"POI 补全失败: {spot.toponym}")
                    # QPS 控速：每次成功请求后间隔，避免后续 spot 全部被限流静默拒答
                    if poi is not None:
                        await asyncio.sleep(0.25)
                    if poi is None:
                        continue
                    if spot.longitude is None and poi.get("location"):
                        try:
                            lng, lat = poi["location"].split(",")[:2]
                            spot.longitude, spot.latitude = float(lng), float(lat)
                        except (ValueError, IndexError):
                            pass
                    photos = [
                        p["url"].replace("http://", "https://")  # 统一 https，避免混合内容
                        for p in (poi.get("photos") or []) if p.get("url")
                    ]
                    if not spot.toponym_image and photos:
                        spot.toponym_image = photos[0]
                    if not spot.toponym_image_list and photos:
                        spot.toponym_image_list = photos
                    # 静态地图兜底：无实景图但坐标已补全 → 生成带定位标记的地图缩略图当封面，
                    # 保证前端每个景点至少有一张可展示的图（替代灰色占位图标）
                    if not spot.toponym_image and spot.longitude is not None:
                        lng, lat = spot.longitude, spot.latitude
                        spot.toponym_image = (
                            f"https://restapi.amap.com/v3/staticmap?location={lng},{lat}"
                            f"&zoom=14&size=750*400&markers=mid,0xFF6E4E,:{lng},{lat}"
                            f"&key={settings.amap_api_key}"
                        )

async def _enrich_routes(plan: TravelItinerary) -> None:
    """高德方向 API 补全每日路线

    每天内按景点顺序两两取驾车路线，拼接各 step 的 polyline，解为平铺
    [lat,lng,...] 数组存入 route_polylines；段失败存空数组，前端回退直线。
    （腾讯方向 API 改用高德：QQ_MAP_KEY 的 WebService 日配额易耗尽 status=121；
    高德/腾讯地图同为 GCJ-02 坐标系，渲染无需转换）
    """
    if not settings.amap_api_key:
        return
    url = "https://restapi.amap.com/v3/direction/driving"
    async with httpx.AsyncClient(timeout=8) as client:
        for day in plan.itinerary_list:
            spots = [
                s for item in day.itinerary for s in item.scenic_spots_list
                if s.latitude is not None and s.longitude is not None
            ]
            routes: list[list[float]] = []
            for a, b in zip(spots, spots[1:]):
                try:
                    res = await client.get(url, params={
                        "origin": f"{a.longitude},{a.latitude}",  # 高德参数经度在前
                        "destination": f"{b.longitude},{b.latitude}",
                        "key": settings.amap_api_key,
                    })
                    steps = res.json()["route"]["paths"][0]["steps"]
                    flat: list[float] = []
                    for step in steps:
                        for pair in step["polyline"].split(";"):
                            lng, lat = pair.split(",")[:2]
                            flat.extend([float(lat), float(lng)])
                    routes.append(flat)
                except Exception:
                    routes.append([])
            if routes:
                day.route_polylines = routes

def _plan_defect(plan: TravelItinerary, expected_days: int = 0) -> str:
    """清单可用性守卫：返回缺陷说明（空串 = 可用）

    背景：generate_plan 对同一会话是**覆盖更新**，且此前不校验内容。实测 DeepSeek 会把
    需求确认语、甚至一句"如需调整…直接告诉我即可"当成行程提炼出来，于是一份已经正确的
    清单被这种空壳覆盖（日志里同一个 plan 被连续覆盖成"（无有效行程内容）"），
    质检随后打出 0/0/0。故落库前先判一次，不可用就不许覆盖。
    """
    days = plan.itinerary_list
    if not days:
        return "清单为空（0 天）"
    spots = sum(len(item.scenic_spots_list) for day in days for item in day.itinerary)
    if spots == 0:
        return f"清单 {len(days)} 天但无任何景点条目"
    if expected_days and len(days) != expected_days:
        return f"天数不符（清单 {len(days)} 天，应为 {expected_days} 天）"
    return ""


async def generate_plan(
        db: AsyncSession, conversation_id: uuid.UUID, markdown: str,
        structure: list[dict] | None = None,
) -> Mapped[str]:
    """提炼结构化行程并落库，返回 plan_id（失败抛异常，由调用方推 error 帧）
    不用 with_structured_output：其内部强制 tool_choice，DeepSeek
    v4 思考模式不支持该参数，会返回400错误。
    改为 bind_tools（tool_choice=auto）+ 解析 tool_calls，兼容思考模式。
    `structure` 为状态机里已过 Pydantic 校验的 itinerary_body。
    LLM提炼失败时，改用代码基于structure构造清单，保证清单一定生成；
    提炼成功则优先使用模型输出，信息更丰富，包含景点描述、坐标等。
    """
    expected_days = len(structure) if structure else 0

    plan: TravelItinerary | None = None
    try:
        bound = get_deepseek_llm().bind_tools([TravelItinerary])
        res = await bound.ainvoke(_JSON_PROMPT + markdown)
        if res.tool_calls:
            plan = TravelItinerary.model_validate(res.tool_calls[0]["args"])
        else:
            app_logger.warning("DeepSeek 未返回结构化行程工具调用")
    except Exception:
        app_logger.warning("行程清单 LLM 提炼失败，转代码兜底", exc_info=True)

    # ★ 提炼结果先过守卫：不可用就当没提炼出来，转代码兜底
    if plan is not None:
        defect = _plan_defect(plan, expected_days)
        if defect:
            app_logger.warning(f"⚠️ LLM 提炼的清单不可用（{defect}），转代码兜底：{plan.title}")
            plan = None

    if plan is None:
        if not structure:
            raise ValueError("行程清单提炼失败，且无可用的结构化行程可兜底")
        plan = _plan_from_structure(structure, markdown)
        app_logger.warning(f"行程清单已由代码兜底生成（{len(plan.itinerary_list)} 天，未经 LLM 提炼）")
        # ★ 兜底清单同样要过守卫：structure 本身是空壳时绝不落库，更不覆盖已有清单
        defect = _plan_defect(plan, expected_days)
        if defect:
            raise ValueError(f"行程清单不可用（{defect}），已放弃落库以保护上一版清单")

    # POI/路线信息增强，补全景点坐标、封面图与每日路线数据
    try:
        await _enrich_spots(plan)   # 补全景点坐标与封面图（高德 POI + 静态地图兜底）
        await _enrich_routes(plan)  # 补全每日路线（前端按天路线图依赖）
    except Exception:
        app_logger.warning("行程POI/路线增强失败（以提炼原始数据落库）", exc_info=True)

    # 同一会话覆盖更新清单，每次重排行程只保留最新版本
    row = (await db.execute(
        select(ItineraryPlan)
        .where(ItineraryPlan.conversation_id == conversation_id)
        .order_by(ItineraryPlan.created_at.desc())
        .limit(1)
    )).scalar_one_or_none()

    if row is not None:
        row.plan_data = plan.model_dump()
        row.status = 1
        app_logger.info(f"行程清单已覆盖更新: {row.plan_id} ({plan.title})")
    else:
        row = ItineraryPlan(
            plan_id=str(uuid.uuid4()),
            conversation_id=conversation_id,
            plan_data=plan.model_dump(),
            status=1,
        )
        db.add(row)
        app_logger.info(f"行程清单已生成: {row.plan_id} ({plan.title})")
    await db.commit()

    # 落库后异步在线质检，事后告警，不阻塞主流程
    schedule_quality_check(plan.model_dump())
    return row.plan_id