"""多段自驾行程 Agent Skill —— Pydantic 数据契约
skill_name = driving_multi_segment

契约总览（前后端字段严格一一对应，禁止改名）：
  入参  DrivingLeg                 ：from + to + travel_date（段数动态 1/Chapter_Two/2_/3…，自驾可不带日期）
  出参  DrivingMultiSegmentPayload ：{skill_name, segments[{segment_id, from, to, date, ok,
                                     distance_km, duration_text, tolls, traffic_lights, summary}]}
  提交  DrivingSubmitPayload       ：{skill_name, segments[{segment_id}]}

交互语义（重要，与航班/高铁不同）：
  1. **只读确认**：每段只给**一条**高德驾车路线（默认策略的最优路线），用户**不能选路线策略**，
     只能整批【确认】（提交项只带 segment_id，没有 option_id）；
  2. **不做任何校验**：不比对段时间先后、不校验路线可行性、不锁位；
  3. 单段查询失败 / 城市解析不出经纬度或城市名 → 该段 ok=False（前端置灰），绝不编造数字。
"""
from typing import Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
)

# Agent / 前端 UI Skill 的注册键（出参首字段，前端据此匹配渲染组件）
DRIVING_MULTI_SEGMENT_SKILL = "driving_multi_segment"


class DrivingLeg(BaseModel):
    """【入参】单段行程（段数由用户行程决定，1/Chapter_Two/2_/3 段均可，不写死）"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    # `from` 是 Python 关键字，故字段名用 from_ + 别名 "from"，保证 JSON 契约里仍是 from
    from_: str = Field(alias="from", description="该段出发城市，如 北京")
    to: str = Field(description="该段到达城市，如 上海")
    # 自驾不依赖发车时刻，日期字段保留但允许为空（仅作展示，不参与任何校验）
    travel_date: str = Field(
        default="",
        validation_alias=AliasChoices("travel_date", "date"),
        description="该段出行日期 YYYY-MM-DD（自驾可不填，留空即不展示）",
    )


class DrivingSegment(BaseModel):
    """【出参】一段行程（segments 元素）；from 同样用别名保证 JSON 契约一致"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    segment_id: str = Field(description="段唯一标识，按 legs 顺序生成：s1 / s2 / s3 …")
    from_: str = Field(alias="from", description="该段出发城市")
    to: str = Field(description="该段到达城市")
    date: str = Field(default="", description="该段出行日期 YYYY-MM-DD；未知时留空")
    ok: bool = Field(
        default=False,
        description="该段是否查询成功（成功即前端可继续；失败=false，前端置灰该段并说明原因）",
    )
    distance_km: float = Field(default=0, description="驾车总距离（公里）；未解析到时为 0")
    duration_text: str = Field(default="", description="预计耗时 HH:mm；未解析到时留空")
    tolls: float = Field(default=0, description="过路费（元）；未解析到或免费时为 0")
    traffic_lights: int = Field(default=0, description="红绿灯数；未解析到时为 0")
    summary: str = Field(default="", description="关键路段摘要（前几条导航指令）；无则留空")


class DrivingMultiSegmentPayload(BaseModel):
    """【出参】Agent 下发前端 UI Skill 的完整 payload（segments 长度动态）"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    skill_name: Literal["driving_multi_segment"] = DRIVING_MULTI_SEGMENT_SKILL
    segments: list[DrivingSegment] = Field(
        default_factory=list, description="全部行程段，顺序与入参 legs 完全一致"
    )

class DrivingSubmitSelection(BaseModel):
    """【提交】前端回传的单段确认结果（只读确认：无路线策略、无班次号，故仅需 segment_id）"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    segment_id: str = Field(description="段标识，必须存在于下发 payload 的 segments 中")


class DrivingSubmitPayload(BaseModel):
    """【提交】前端一次性全局提交的 payload（整批确认，不选策略）"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    skill_name: Literal["driving_multi_segment"] = DRIVING_MULTI_SEGMENT_SKILL
    segments: list[DrivingSubmitSelection] = Field(
        description="全部段的确认项，顺序不限（后端按 segment_id 归位）"
    )


class DrivingSubmitResult(BaseModel):
    """【提交回执】只反映基础合法性校验结果（段是否存在、是否覆盖完整）"""

    ok: bool = Field(description="全部段校验通过为 True")
    code: str = Field(
        description=(
            "结果码：OK / EMPTY_SELECTION / SEGMENT_NOT_FOUND / INCOMPLETE / NO_PAYLOAD / "
            "INVALID_PAYLOAD / STEP_MISMATCH"
        )
    )
    msg: str = Field(description="面向 Agent 的中文说明")
    accepted: list[DrivingSubmitSelection] = Field(
        default_factory=list, description="校验通过的确认项（原样回显）"
    )
    # 已确认段的展示文本（"北京→上海 自驾 1210.5km 12:30"）：提交成功后由路由侧拼成
    # 一条合成发言续跑住宿步，故必须把段文本带出本模块
    texts: list[str] = Field(
        default_factory=list, description="已确认各段的展示文本，按行程段顺序排列"
    )