"""
TravelState 状态定义
使用 TypedDict + Annotated 实现类型安全和状态合并
"""
from typing import Annotated, Literal, Optional
from typing_extensions import TypedDict, NotRequired
from operator import add
from langchain.agents import AgentState

# ============== 枚举类型定义 ==============

PlanningStep = Literal[
    "requirement_collection",  # 需求收集
    "destination_recommendation",  # 目的地推荐
    "transport_planning",  # 交通规划
    "accommodation_planning",  # 住宿规划
    "food_planning",  # 餐饮规划
    "itinerary_generation",  # 行程生成
    "budget_summarization",  # 预算汇总
    "report_generation"  # 报告生成
]

TravelStyle = Literal[
    "relaxation",  # 休闲度假
    "culture",  # 文化探索
    "adventure",  # 户外冒险
    "food"  # 美食之旅
]

BudgetLevel = Literal[
    "economy",  # 经济型（< 3000元/人）
    "comfort",  # 舒适型（3000-8000元/人）
    "luxury"  # 豪华型（> 8000元/人）
]

TransportType = Literal[
    "flight",  # 航班
    "train",  # 高铁/火车
    "driving",  # 自驾
    "local"  # 本地游玩（出发地=目的地，零城际段）
]

AccommodationType = Literal[
    "star_hotel",  # 星级酒店
    "economy_hotel",  # 经济酒店
    "hostel",  # 民宿
    "youth_hostel"  # 青年旅社
]

FoodType = Literal[
    "specialty",  # 特色美食
    "chain",  # 连锁快餐
    "local"  # 本地小吃
]


# ============== 用户需求结构 ==============

class UserRequirement(TypedDict):
    """用户旅行需求"""
    departure_city: str  # 出发地点
    destination: str  # 目的地
    departure_date: str  # 出发日期（YYYY-MM-DD）
    travel_days: int  # 出行天数
    adult_count: int  # 成人数量
    children_count: int  # 儿童数量
    budget_min: Optional[float]  # 预算下限
    budget_max: Optional[float]  # 预算上限
    budget_level: BudgetLevel  # 预算等级
    travel_styles: list[TravelStyle]  # 旅行风格（多选）
    special_needs: Optional[str]  # 特殊需求（文本）
    # 表单预选项：表单已勾选时随需求一并记录，后续步骤免重复询问
    transport_types: NotRequired[list[str]]  # 期望交通方式 flight/train/driving
    food_preferences: NotRequired[list[str]]  # 餐饮偏好 specialty/chain/local
    accommodation_types: NotRequired[list[str]]  # 住宿偏好 star_hotel/economy_hotel/hostel/youth_hostel


# ============== 查询结果结构 ==============

class DestinationInfo(TypedDict):
    """目的地信息"""
    name: str  # 目的地名称
    description: str  # 描述
    weather_info: Optional[str]  # 天气信息
    attractions: list[str]  # 景点列表
    estimated_cost: Optional[float]  # 预估花费


class TransportInfo(TypedDict):
    """交通信息"""
    transport_type: TransportType
    details: str  # 详细信息（航班号/车次等）
    departure_time: str  # 出发时间
    arrival_time: str  # 到达时间
    duration: str  # 行程时间（HH:MM）
    price: float  # 价格（人/天）


class AccommodationInfo(TypedDict):
    """住宿信息"""
    name: str  # 住宿名称
    type: AccommodationType  # 住宿类型
    location: str  # 位置
    price_per_night: float  # 价格（人/天）
    rating: Optional[float]  # 评分（可选）
    amenities: list[str]  # 设施（如：WiFi、Parking、Swimming Pool等，可选）


class FoodInfo(TypedDict):
    """餐饮信息"""
    type: FoodType  # 餐饮类型
    recommendations: list[str]  # 推荐餐厅/美食
    estimated_daily_cost: float  # 预估花费（人/天）


class ItineraryDay(TypedDict):
    """单日行程"""
    day_number: int  # 天数（从1开始）
    activities: list[str]  # 活动列表
    meals: list[str]  # 餐饮列表
    accommodation: str  # 住宿名称


class BudgetBreakdown(TypedDict):
    """预算明细"""
    transport: float  # 交通（人/天）
    accommodation: float  # 住宿（人/天）
    food: float  # 餐饮（人/天）
    attractions: float  # 景点（人/天）
    attractions_source: NotRequired[str]  # 门票金额来源："real"（真实票价）/ "estimated"（估算）
    misc: float  # 其他（人/天）
    total: float  # 总预算（人/天）


# ============== 自定义 Reducer ==============

CLEAR = "__CLEAR__"
def _keep_last_non_none(current, new):
    """"非空优先"合并器（专用于 itinerary / itinerary_body / budget 这类"产物字段"）

    为什么需要：
    LangGraph 默认每个 State 字段用 LastValue 通道，也就是"同一步只能写一次"。
    但一个步骤内可能被多把工具同时更新——比如一个工具写空值（表示重置），
    另一个工具写有效结果，两者并发就会抛：
        InvalidUpdateError: At key 'budget': Can receive only one value per step

    业务逻辑：空值不覆盖已有内容——既避免图直接崩溃，也不会丢已算出的产物；
    真要清空就写 CLEAR（见上），两种"空"必须分得开。
    """
    if isinstance(new, str) and new == CLEAR:   # ★ 显式清空
        return None
    return current if new is None else new


from app.utils.logger import app_logger

def _last_write_wins(current, new):
    """current_step 专用合并器：同一步被写多次时取后者，值不同时留一条告警

    为什么需要：默认通道"一步只能写一次"，两把工具并行推进就直接炸
        InvalidUpdateError: At key 'current_step': Can receive only one value per step
    （当初被迫造 select_accommodation_food_tool 这种组合工具，就是为了绕开它）。
    改为容错：值相同（最常见——两把工具都推进到同一步）直接通过；
    值不同按写入顺序取后者，并打 WARNING 便于复盘。注意**不能**改成"取更靠后的步骤"：
    回退工具正是要往前写，那样会把 go_back 整个废掉。
    """
    if new is None:
        return current
    if current is not None and current != new:
        app_logger.warning(f"⚠️ current_step 本步被并发写入：{current} → {new}（按写入顺序取后者）")
    return new


# ============== 主状态定义 ==============

class TravelState(AgentState):
    """
    旅行规划系统主状态

    继承自 AgentState，自动获得：
    - messages: 对话消息列表
    - 其他 Agent 通用字段
    """

    # ========== 流程控制 ==========
    current_step: NotRequired[Annotated[PlanningStep, _last_write_wins]]  # 当前步骤
    # 确认模式真源：由 record_requirement_tool 写入（表单是否勾选"全程交给 AI 自动确认"）
    # 供 middleware 渲染成语义变量注入各步骤 prompt
    auto_confirm: NotRequired[bool]

    # ========== 用户输入 ==========
    user_requirement: NotRequired[UserRequirement]

    # ========== 用户选择 ==========
    selected_destination: NotRequired[str]
    selected_transport: NotRequired[TransportType]
    selected_accommodation_types: NotRequired[list[AccommodationType]]
    selected_food_types: NotRequired[list[FoodType]]
    # 多目的地逐段/逐城确认留痕
    selected_transport_legs: NotRequired[list[str]]
    selected_hotels: NotRequired[list[str]]
    # 段级循环：段列表游标 + 已确认结果
    leg_cursor: NotRequired[int]
    leg_selections: NotRequired[dict[str, str]]
    stay_cursor: NotRequired[int]

    # ========== 查询结果 ==========
    destination_options: NotRequired[list[DestinationInfo]]
    transport_options: NotRequired[list[TransportInfo]]
    accommodation_options: NotRequired[list[AccommodationInfo]]
    food_options: NotRequired[list[FoodInfo]]

    # ========== 最终结果 ==========
    # itinerary / budget 挂 Annotated 合并器：避免并发写空值把已有产物覆盖掉
    itinerary: NotRequired[Annotated[list[ItineraryDay], _keep_last_non_none]]
    itinerary_body: NotRequired[Annotated[list[dict], _keep_last_non_none]]
    budget: NotRequired[Annotated[BudgetBreakdown, _keep_last_non_none]]
    report: NotRequired[str]  # 旅行报告（Markdown）

    # ========== 审批状态 ==========
    approval_pending: NotRequired[bool]
    approval_reason: NotRequired[str]

    # ========== 元数据 ==========
    user_id: NotRequired[str]
    session_id: NotRequired[str]
    created_at: NotRequired[float]
    updated_at: NotRequired[float]

    # 与 leg_selections / selected_hotels 同一次写入：那两个存展示文本（给人看），
    # 这两个存纯数字（给代码算）。预算侧因此不必再从展示文本里抠价——
    # 抠自己拼的字符串是反模式：文案一改，抠价就静默失效
    leg_prices: NotRequired[dict[str, float]]  # 段名（"北京→天津"）→ 人均票价（元）
    stay_prices: NotRequired[dict[str, float]]  # 城市名 → 每晚房价（元）


