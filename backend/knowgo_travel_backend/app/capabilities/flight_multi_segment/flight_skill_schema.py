"""多段航班行程 Agent Skill —— Pydantic 数据契约
skill_name = flight_multi_segment

契约总览（前后端字段严格一一对应，禁止改名）：
  入参  FlightLeg                 ：from + to + travel_date（段数动态 1/2/3…）
  出参  FlightMultiSegmentPayload ：{skill_name, segments[{segment_id, from, to, date, flight_list[]}]}
  提交  FlightSubmitPayload       ：{skill_name, segments[{segment_id, option_id, flight_no}]}

身份约定（重要）：航班号**不是唯一身份**——同一天同一航班号可能执行不同航段/时刻，
故选中、校验、回写一律以 FlightOption.option_id（= 航班号-起飞时刻-到达机场）为准。

职责边界（重要，不可越界）：
  1. 本 Skill 只做「并行查询已有航班工具 + 组装候选班次」，不做逐段确认、不锁票；
  2. **不做任何航班时间冲突校验**（不比对到达/起飞先后）；
  3. 提交侧只做基础合法性校验（segment_id / option_id 是否存在），不校验时间。
"""
from typing import Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
)

# Agent / 前端 UI Skill 的注册键（出参首字段，前端据此匹配渲染组件）
FLIGHT_MULTI_SEGMENT_SKILL = "flight_multi_segment"


class FlightLeg(BaseModel):
    """【入参】单段行程（段数由用户行程决定，1/2/3 段均可，不写死）"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    # `from` 是 Python 关键字，故字段名用 from_ + 别名 "from"，保证 JSON 契约里仍是 from
    from_: str = Field(alias="from", description="该段出发城市，如 北京")
    to: str = Field(description="该段到达城市，如 上海")
    travel_date: str = Field(
        default="",
        validation_alias=AliasChoices("travel_date", "date"),
        description="该段乘机日期 YYYY-MM-DD；缺省时由代码按行程时序推算后传入",
    )


class FlightOption(BaseModel):
    """【出参】单个航班（flight_list 元素）；字段名与前端 TS 类型逐字一致"""

    flight_no: str = Field(description="航班号，如 CA1501")
    dep_time: str = Field(description="计划起飞时刻 HH:mm")
    arr_time: str = Field(description="计划到达时刻 HH:mm")
    dep_airport: str = Field(description="出发机场三字码或名称，如 PEK")
    arr_airport: str = Field(description="到达机场三字码或名称，如 SHA")
    price: float = Field(default=0, description="票价（元/人）；数据源未返回价格时为 0")
    duration: str = Field(default="", description="历时 HH:mm；无法解析时留空")
    # 身份字段：航班号在同一天可能不唯一（不同航段/时刻），故选中/校验/回写一律以
    # option_id 为身份（= 航班号-起飞时刻-到达机场），前端不得用 flight_no 当身份
    option_id: str = Field(
        default="",
        description="该航班的段内唯一标识：`航班号-起飞时刻-到达机场`（如 CA1501-08:00-SHA）",
    )


class FlightSegment(BaseModel):
    """【出参】一段行程（segments 元素）；from 同样用别名保证 JSON 契约一致"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    segment_id: str = Field(description="段唯一标识，按 legs 顺序生成：s1 / s2 / s3 …")
    from_: str = Field(alias="from", description="该段出发城市")
    to: str = Field(description="该段到达城市")
    date: str = Field(description="该段乘机日期 YYYY-MM-DD")
    flight_list: list[FlightOption] = Field(
        default_factory=list,
        description="该段候选航班（已按起飞时间升序）；查询失败时为空数组，前端置灰该段",
    )
    # ★ 段级参考价：票价接口只给"本航线 XX 元起"、对不到具体航班号时放这里。
    #   **不**把它挨个填进 flight_list 的 price（同一航线各班票价不同，填了就是编造）。
    #   前端可在段标题旁展示"¥XXX 起"；为 0 表示该段票价未知。
    low_price: float = Field(
        default=0, description="该段航线最低参考票价（元/人）；未取到为 0"
    )


class FlightMultiSegmentPayload(BaseModel):
    """【出参】Agent 下发前端 UI Skill 的完整 payload（segments 长度动态）"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    skill_name: Literal["flight_multi_segment"] = FLIGHT_MULTI_SEGMENT_SKILL
    segments: list[FlightSegment] = Field(
        default_factory=list, description="全部行程段，顺序与入参 legs 完全一致"
    )


class FlightSubmitSelection(BaseModel):
    """【提交】前端回传的单段选中结果（身份 = option_id；flight_no 仅作展示/日志）"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    segment_id: str = Field(description="段标识，必须存在于下发 payload 的 segments 中")
    option_id: str = Field(
        default="",
        description="选中航班的段内唯一标识（下发 payload 里该航班的 option_id）",
    )
    flight_no: str = Field(default="", description="航班号（随 option_id 一起回显，仅作展示）")


class FlightSubmitPayload(BaseModel):
    """【提交】前端一次性全局提交的 payload（逐段单独确认已取消）"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    skill_name: Literal["flight_multi_segment"] = FLIGHT_MULTI_SEGMENT_SKILL
    segments: list[FlightSubmitSelection] = Field(
        description="全部段的选中结果，顺序不限（后端按 segment_id 归位）"
    )


class FlightSubmitResult(BaseModel):
    """【提交回执】只反映基础合法性校验结果，不含任何时间连续性问题"""

    ok: bool = Field(description="全部段校验通过为 True")
    code: str = Field(
        description=(
            "结果码：OK / EMPTY_SELECTION / MISSING_OPTION_ID / SEGMENT_NOT_FOUND / "
            "FLIGHT_NOT_FOUND / INCOMPLETE / NO_PAYLOAD / INVALID_PAYLOAD / STEP_MISMATCH"
        )
    )
    msg: str = Field(description="面向 Agent 的中文说明")
    accepted: list[FlightSubmitSelection] = Field(
        default_factory=list, description="校验通过的选中项（原样回显）"
    )
    # 已选定段的展示文本（"北京→上海 CA1501 08:00-10:00 PEK→SHA"）：
    # 提交成功后由路由侧拼成一条合成发言续跑住宿步，故必须把段文本带出本模块
    texts: list[str] = Field(
        default_factory=list, description="已选定各段的展示文本，按行程段顺序排列"
    )