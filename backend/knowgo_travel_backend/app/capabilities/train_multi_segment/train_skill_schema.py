"""
多段高铁行程 Agent Skill —— Pydantic 数据契约
skill_name = train_multi_segment

契约总览（前后端字段严格一一对应，禁止改名）：
  入参（查询工具 `query_train_multi_segment_tool`）：legs[{from, to, travel_date}] + seat_type + max_options_per_leg
  出参  TrainMultiSegmentPayload：{skill_name, segments[{segment_id, from, to, date, train_list[]}]}
  提交  TrainSubmitPayload      ：{skill_name, segments[{segment_id, option_id, trainNo}]}

身份约定（重要）：车次号**不是唯一身份**——12306 真实数据里同一段会出现同号不同终到站的车
（如 G1308 08:36 分别终到 石家庄 / 正定机场），故选中、校验、回写一律以 TrainOption.option_id
（= 车次-发车时刻-到达站）为准。

职责边界（重要，不可越界）：
  1. 本 Skill 只做「并行查询已有 12306 工具 + 组装候选班次」，不做逐段确认、不锁票；
  2. **不做任何列车时间冲突校验**（不比对到站/发车先后，前端同样不校验）；
  3. 提交侧只做基础合法性校验（segment_id / trainNo 是否存在），不校验时间。
"""
from typing import Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
)

# Agent / 前端 UI Skill 的注册键（出参首字段，前端据此匹配渲染组件）
TRAIN_MULTI_SEGMENT_SKILL = "train_multi_segment"


class TrainLeg(BaseModel):
    """【入参】单段行程（段数由用户行程决定，2/3/4 段均可，不写死）"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    # `from` 是 Python 关键字，故字段名用 from_ + 别名 "from"，保证 JSON 契约里仍是 from
    from_: str = Field(alias="from", description="该段出发城市，如 石家庄")
    to: str = Field(description="该段到达城市，如 北京")
    travel_date: str = Field(
        default="",
        validation_alias=AliasChoices("travel_date", "date"),
        description="该段乘车日期 YYYY-MM-DD；缺省时由 Agent 按行程时序推算后传入",
    )


class TrainOption(BaseModel):
    """【出参】单趟车次（train_list 元素）；字段名与前端 TS 类型逐字一致"""

    trainNo: str = Field(description="车次号，如 D40 / G547 / G1952")
    startTime: str = Field(description="发车时刻 HH:mm")
    endTime: str = Field(description="到达时刻 HH:mm")
    startStation: str = Field(description="出发站中文名，如 石家庄北")
    endStation: str = Field(description="到达站中文名，如 北京丰台")
    duration: str = Field(description="历时 HH:mm")
    price: float = Field(description="票价（元/人），取 seatType 对应席别")
    seatType: str = Field(description="座位类型，如 二等座 / 一等座")
    ticketStatus: str = Field(description="余票状态：有票 / 无票 / 候补 / N张")
    # 身份字段：同一车次号在同一段内可能有多趟（12306 真实数据，
    # 如 G1308 08:36 分别终到「石家庄」「正定机场」两趟），故选中/校验/回写一律以
    # option_id 为身份，**禁止用 trainNo 当身份**（否则两趟会被同时勾选、回写串行）
    option_id: str = Field(
        default="",
        description="该趟车的段内唯一标识：`车次-发车时刻-到达站`（如 G1308-08:36-正定机场）",
    )

    @field_serializer("price")
    def _serialize_price(self, value: float) -> int | float:
        """整数票价去掉小数尾（52.0 → 52），避免前端显示 ¥52.0（返回类型须为 int|float）"""
        return int(value) if float(value).is_integer() else float(value)

class TrainSegment(BaseModel):
    """【出参】一段行程（segments 元素）；from 同样用别名保证 JSON 契约一致"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    segment_id: str = Field(description="段唯一标识，按 legs 顺序生成：s1 / s2 / s3 …")
    from_: str = Field(alias="from", description="该段出发城市")
    to: str = Field(description="该段到达城市")
    date: str = Field(description="该段乘车日期 YYYY-MM-DD")
    train_list: list[TrainOption] = Field(
        default_factory=list,
        description="该段候选车次（已按发车时间升序）；查询失败时为空数组，前端置灰该段",
    )


class TrainMultiSegmentPayload(BaseModel):
    """【出参】Agent 下发前端 UI Skill 的完整 payload（segments 长度动态）"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    skill_name: Literal["train_multi_segment"] = TRAIN_MULTI_SEGMENT_SKILL
    segments: list[TrainSegment] = Field(
        default_factory=list, description="全部行程段，顺序与入参 legs 完全一致"
    )


class TrainSubmitSelection(BaseModel):
    """【提交】前端回传的单段选中结果（身份 = option_id；trainNo 仅作展示/日志）"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    segment_id: str = Field(description="段标识，必须存在于下发 payload 的 segments 中")
    option_id: str = Field(
        default="",
        description="选中车次的段内唯一标识（下发 payload 里该趟车的 option_id）；"
                    "同一车次号可能对应多趟，故以本字段为准",
    )
    trainNo: str = Field(default="", description="车次号（随 option_id 一起回显，仅作展示）")


class TrainSubmitPayload(BaseModel):
    """【提交】前端一次性全局提交的 payload（逐段单独确认已取消）"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    skill_name: Literal["train_multi_segment"] = TRAIN_MULTI_SEGMENT_SKILL
    segments: list[TrainSubmitSelection] = Field(
        description="全部段的选中结果，顺序不限（后端按 segment_id 归位）"
    )


class TrainSubmitResult(BaseModel):
    """【提交回执】只反映基础合法性校验结果，不含任何时间连续性问题"""

    ok: bool = Field(description="全部段校验通过为 True")
    code: str = Field(
        description=(
            "结果码：OK / EMPTY_SELECTION / SEGMENT_NOT_FOUND / TRAIN_NOT_FOUND / "
            "INCOMPLETE / NO_PAYLOAD / INVALID_PAYLOAD / MISSING_OPTION_ID / STEP_MISMATCH"
        )
    )
    msg: str = Field(description="面向 Agent 的中文说明")
    accepted: list[TrainSubmitSelection] = Field(
        default_factory=list, description="校验通过的选中项（原样回显）"
    )
    # 已选定段的展示文本（"A→B G547 08:00-12:00 二等座553元"）：
    # 提交成功后由路由侧拼成一条合成发言续跑住宿步，故必须把段文本带出本模块
    texts: list[str] = Field(
        default_factory=list, description="已选定各段的展示文本，按行程段顺序排列"
    )