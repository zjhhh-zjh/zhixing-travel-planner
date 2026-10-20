"""
多段住宿选择 Agent Skill —— Pydantic 数据契约
skill_name = hotel_multi_segment

契约总览（前后端字段严格一一对应，禁止改名）：
  入参（查询工具 `query_hotel_multi_segment_tool`）：
                                segments[{city, checkInDate, checkOutDate, guestCount, priceLevel}]
                                段数动态（= 行程目的地数量，2/3/多段均可，不写死）
  出参  HotelMultiSegmentPayload：{skill_name, segments[{segment_id, city, checkInDate, checkOutDate,
                                   guestCount, priceLevel, mode, hotel_list[], brand_list[]}]}
  提交  HotelMultiSegmentSubmitPayload：{skill_name, segments[{segment_id, mode, hotel_id, brand_id}]}

双模式（**每一段独立判定**，段与段之间互不影响，由后端解析结果自动识别）：
  - mode="hotel"：该段拿到真实门店列表 → 前端渲染门店单选卡片
  - mode="brand"：该段实时报价不可用、没有门店 → 前端渲染品牌单选卡片（顶部加提示文案）
  每段内部 hotel_list 与 brand_list **二选一**：非当前模式的一方恒为空数组。

身份约定（同高铁 option_id 的思路）：候选 id **段内唯一**，格式为 `{segment_id}-{序号}`，
  如 h1-1（第 1 段第 1 家门店）、b2-3（第 2 段第 3 个品牌）。段与段之间不共号，
  提交回传一律以该 id 为准，避免多段同号候选串段。

职责边界（重要，不可越界）：
  1. 本 Skill 只做「按段并行调用已有酒店 MCP 工具 + 解析原始返回 + 组装标准 payload」，
     **不自己实现 MCP 服务**、不做排序推荐、不做价格/预算二次计算；
  2. 提交侧只做基础合法性校验（段是否存在、mode 是否匹配、id 是否在该段候选内、是否每段都选了），
     **不做任何日期/时间校验**，也不做库存、支付等业务校验。
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

# Agent / 前端 UI Skill 的注册键（出参首字段，前端据此匹配渲染组件）
HOTEL_MULTI_SEGMENT_SKILL = "hotel_multi_segment"

# 单段渲染模式：hotel=真实门店候选；brand=品牌兜底候选
HotelMode = Literal["hotel", "brand"]


class HotelOption(BaseModel):
    """【出参】单个真实酒店门店（hotel_list 元素）；字段名与前端 TS 类型逐字一致"""

    hotel_id: str = Field(description="门店候选唯一标识，多段下发时按段内序号生成：h1-1 / h2-3 …")
    hotelName: str = Field(description="酒店门店名称，如 尚客优酒店（石家庄长安区谈固北大街店）")
    totalPrice: float = Field(description="住宿总价（元），整个入住期间合计")
    pricePerNight: float = Field(description="每晚单价（元/晚）")
    starRating: float = Field(description="评分 / 星级，如 2.0 / 4.5")
    address: str = Field(description="门店地址（含城区）")

    @field_serializer("totalPrice", "pricePerNight")
    def _serialize_price(self, value: float) -> int | float:
        """整数价格去掉小数尾（302.0 → 302），避免前端显示 ¥302.0（返回类型须为 int|float）"""
        return int(value) if float(value).is_integer() else float(value)


class HotelBrand(BaseModel):
    """【出参】单个酒店品牌（brand_list 元素，实时报价不可用时的兜底候选）"""

    brand_id: str = Field(description="品牌候选唯一标识，多段下发时按段内序号生成：h2-1 / b2-3 …")
    brandName: str = Field(description="品牌名称，如 如家 / 汉庭 / 7 天")


class HotelSegmentInput(BaseModel):
    """【入参】单段住宿需求（段数由行程目的地数量决定，不写死）"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    city: str = Field(description="该段住宿城市中文名，如 石家庄")
    checkInDate: str = Field(description="该段入住日期 YYYY-MM-DD")
    checkOutDate: str = Field(description="该段离店日期 YYYY-MM-DD（住 N 晚 = 离店-入住）")
    guestCount: int = Field(default=1, ge=1, description="入住人数（成人），默认 1")
    priceLevel: str = Field(
        default="经济型", description="房型档位（经济型 / 舒适型 / 星级酒店 / 民宿 / 青年旅社）"
    )


class HotelMultiSegmentItem(BaseModel):
    """【出参】单段住宿（segments 元素）：段内自带 mode，hotel_list / brand_list 二选一"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    segment_id: str = Field(description="段唯一标识，按入参顺序生成：h1 / h2 / h3 …")
    city: str = Field(description="该段住宿城市")
    checkInDate: str = Field(description="该段入住日期 YYYY-MM-DD")
    checkOutDate: str = Field(description="该段离店日期 YYYY-MM-DD")
    guestCount: int = Field(description="入住人数")
    priceLevel: str = Field(description="房型档位，如 经济型")
    mode: HotelMode = Field(
        description='该段渲染模式：hotel=真实门店列表；brand=品牌兜底列表（实时报价不可用）'
    )
    hotel_list: list[HotelOption] = Field(
        default_factory=list, description='该段门店候选；mode="brand" 时恒为空数组'
    )
    brand_list: list[HotelBrand] = Field(
        default_factory=list, description='该段品牌候选；mode="hotel" 时恒为空数组'
    )


class HotelMultiSegmentPayload(BaseModel):
    """【出参】Agent 下发前端 UI Skill 的完整 payload（segments 长度动态，前端按长度出步骤）"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    skill_name: Literal["hotel_multi_segment"] = HOTEL_MULTI_SEGMENT_SKILL
    segments: list[HotelMultiSegmentItem] = Field(
        default_factory=list, description="全部住宿段，顺序与入参 segments 完全一致"
    )

class HotelSubmitSelection(BaseModel):
    """【提交】前端回传的单段选中结果（单选：门店 或 品牌）"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    segment_id: str = Field(description="段标识，必须存在于下发 payload 的 segments 中")
    mode: HotelMode = Field(default="hotel", description="该段模式，必须与下发 payload 一致")
    hotel_id: str = Field(default="", description='mode="hotel" 时必填，如 h1-1')
    brand_id: str = Field(default="", description='mode="brand" 时必填，如 b2-1')


class HotelMultiSegmentSubmitPayload(BaseModel):
    """【提交】前端一次性全局提交的 payload（不做逐段确认，只在预览页提交一次）"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    skill_name: Literal["hotel_multi_segment"] = HOTEL_MULTI_SEGMENT_SKILL
    segments: list[HotelSubmitSelection] = Field(
        description="全部住宿段的选中结果，顺序不限（后端按 segment_id 归位）"
    )


class HotelMultiSegmentSubmitResult(BaseModel):
    """【提交回执】只反映基础合法性校验结果（不含任何日期/时间校验）"""

    ok: bool = Field(description="全部段校验通过为 True")
    code: str = Field(
        description=(
            "结果码：OK / NO_PAYLOAD / EMPTY_SELECTION / INVALID_PAYLOAD / SEGMENT_NOT_FOUND / "
            "MODE_MISMATCH / MISSING_HOTEL_ID / MISSING_BRAND_ID / HOTEL_NOT_FOUND / "
            "BRAND_NOT_FOUND / INCOMPLETE / STEP_MISMATCH"
        )
    )
    msg: str = Field(description="面向 Agent 的中文说明")
    accepted: list[HotelSubmitSelection] = Field(
        default_factory=list, description="校验通过的选中项（原样回显）"
    )
    # 已选各段的展示文本（如「石家庄（09-20 至 09-22）：尚客优酒店 151元/晚」）：
    # 提交成功后由路由侧拼成一条合成发言续跑餐饮/行程步骤，故必须把展示文本带出本模块
    texts: list[str] = Field(
        default_factory=list, description="已选定各段的展示文本，按行程段顺序排列"
    )
    # 已选各段的**每晚房价**（城市 → 元/晚），与 texts 同一次产出：
    # texts 给人看、prices 给代码算，预算侧不必再从"358元/晚"这串文案里抠数字。
    # 品牌兜底段没有实时报价 → 该城不出现在本字典里（"没有"与"0 元"必须可区分）
    prices: dict[str, float] = Field(
        default_factory=dict, description="已选定各段的每晚房价（城市 → 元/晚）；品牌兜底段不计入"
    )
    # 落库后状态机实际推进到的步骤（food_planning / itinerary_generation）：
    # 提交路由据此挑"续跑反馈文案"（住宿已确认 → 正在生成行程报告），避免文案与界面自相矛盾
    next_step: str = Field(default="", description="落库后状态机推进到的下一步骤")


__all__ = [
    "HOTEL_MULTI_SEGMENT_SKILL",
    "HotelMode",
    "HotelSegmentInput",
    "HotelMultiSegmentItem",
    "HotelMultiSegmentPayload",
    "HotelSubmitSelection",
    "HotelMultiSegmentSubmitPayload",
    "HotelMultiSegmentSubmitResult",
    "HotelOption",
    "HotelBrand",
]