"""
状态转换工具
用于 Handoffs 流程中的步骤跳转和数据记录
"""
from datetime import datetime
from langchain.tools import tool, ToolRuntime
from langchain_core.messages import ToolMessage
from langgraph.types import Command  # ★ Command(update=…)：写 state + 推进步骤的唯一手段
from app.core.state import (
    TravelState,
    UserRequirement,
    CLEAR,  # 合并器哨兵：写 CLEAR 才真正清空（写 None 会被 _keep_last_non_none 保留）
)
from app.utils.logger import app_logger
from typing import Annotated, Literal, Optional
# 行程正文结构化校验（Pydantic v2，校验上下文传 travel_days）
from pydantic import BaseModel, Field, ValidationError, ValidationInfo, model_validator

_FORM_DAYS_LABEL = "旅行天数"
_FORM_DEST_PREFIX = "我想去"
_FORM_DEST_SUFFIX = "旅行"

def _value_after(text: str, label: str) -> str:
    """取「…{label}：值」冒号后到行尾的内容，无匹配行返回空串"""
    for line in str(text or "").splitlines():
        pos = line.find(label)
        if pos < 0:
            continue
        rest = line[pos + len(label):].lstrip()
        # 兼容中英文冒号，标签后非冒号则忽略该行
        if rest[:1] in ("：", ":"):
            return rest[1:].strip()
    return ""

def _leading_int(text: str) -> int | None:
    """提取字符串开头整数，如"5 天"→5；开头非数字返回None"""
    digits = ""
    for ch in str(text or "").strip():
        if not ch.isdecimal():
            break
        digits += ch
    return int(digits) if digits else None

def _between(text: str, prefix: str, suffix: str) -> str:
    """提取同一行内prefix与其后最近suffix之间的文本，找不到返回空串"""
    for line in str(text or "").splitlines():
        start = line.find(prefix)
        if start < 0:
            continue
        start += len(prefix)
        end = line.find(suffix, start + 1)
        if end > start:
            return line[start:end]
    return ""

def _form_travel_days(state) -> int | None:
    """从本轮用户表单消息读取旅行天数，无表单行返回None，不干预原有逻辑"""
    # 倒序遍历消息，取本轮最新人类消息作为表单提交文本
    for m in reversed((state or {}).get("messages") or []):
        if getattr(m, "type", None) == "human":
            return _leading_int(_value_after(
                str(getattr(m, "content", "") or ""), _FORM_DAYS_LABEL))
    return None

def _form_destination(state) -> str | None:
    """从本轮用户表单消息读取目的地，无表单行返回None"""
    for m in reversed((state or {}).get("messages") or []):
        if getattr(m, "type", None) == "human":
            found = _between(str(getattr(m, "content", "") or ""),
                             _FORM_DEST_PREFIX, _FORM_DEST_SUFFIX).strip()
            return found or None
    return None

# ============== 1️.需求收集工具 ==============

@tool
def record_requirement_tool(
        # 出发城市
        departure_city: str,
        # 出发日期
        departure_date: str,
        # 旅行天数
        travel_days: int,
        # 预算下限（元/人）
        budget_min: float,
        # 预算上限（元/人）
        budget_max: float,
        # 旅行风格偏好
        travel_styles: list[str],  # 传入字符串列表，工具内转换
        # 特殊需求（可选）
        special_needs: str = "",
        # 成人数量（默认1）
        adult_count: Optional[int] = 1,
        # 儿童数量（< 12 岁）
        children_count: Optional[int] = 0,
        # 目的地（可选）
        destination: Optional[str] = None,
        # 确认模式：表单勾选"全程交给 AI 自动确认"时为 True，未勾选保持 False
        auto_confirm: bool = False,
        # 表单预选交通方式（可多选，可为空）
        transport_types: Optional[list[str]] = None,
        # 表单预选餐饮偏好（可多选，可为空）
        food_preferences: Optional[list[str]] = None,
        # 表单预选住宿偏好（可多选，可为空）
        accommodation_types: Optional[list[str]] = None,

        runtime: ToolRuntime[None, TravelState] = None
) -> Command:
    """
    记录用户旅行需求，并转换到目的地推荐步骤。

    参数(可为空)说明：
    - destination 目的地
    - departure_city 出发地
    - departure_date: 出发日期，格式 YYYY-MM-DD，如 "2025-08-01"
    - travel_days: 出行天数，如 5
    - adult_count: 成人数量
    - children_count: 儿童数量（< 12 岁）
    - budget_min: 预算下限（元/人）
    - budget_max: 预算上限（元/人）
    - travel_styles: 旅行风格列表，可选值：["relaxation", "culture", "adventure", "food"]
    - special_needs: 特殊需求（可选），如 "需要无障碍设施"
    """

    # 天数锁定——表单给了值就以表单为准，模型读错也不污染后续全部步骤
    # ★ 表单天数是权威值：存在就覆盖模型传参（天数一错，住宿/餐饮/预算/正文全跟着漂移）
    _form_days = _form_travel_days(runtime.state)
    if _form_days and int(travel_days or 0) != _form_days:
        app_logger.warning(f"⚠️ 天数以表单为准：模型传 {travel_days} 天 → 表单 {_form_days} 天")
        travel_days = _form_days

    # 目的地锁定——模型不传 destination 时以表单 head 为准（多城串全量保留）
    if not str(destination or "").strip():
        _form_dest = _form_destination(runtime.state)
        if _form_dest:
            app_logger.warning(f"⚠️ 目的地以表单为准：模型未传 → 表单 {_form_dest}")
            destination = _form_dest

    app_logger.info(f"记录用户需求: {departure_date}, {travel_days}天, 预算 {budget_min}-{budget_max}")

    # 验证日期格式
    try:
        datetime.strptime(departure_date, "%Y-%m-%d")
    except ValueError:
        return Command(update={
            "messages": [
                ToolMessage(
                    content="❌ 日期格式错误，请使用 YYYY-MM-DD 格式，如 2025-08-01",
                    tool_call_id=runtime.tool_call_id
                )
            ]
        })

    # 推断预算等级
    avg_budget = (budget_min + budget_max) / 2
    if avg_budget < 3000:
        # ★ 预算等级按区间由代码算，不让模型填
        budget_level = "economy"
    elif avg_budget < 8000:
        budget_level = "comfort"
    else:
        budget_level = "luxury"

    # 构建需求对象
    requirement = UserRequirement(
        departure_city=departure_city,
        destination=destination,
        departure_date=departure_date,
        travel_days=travel_days,
        adult_count=adult_count,
        children_count=children_count,
        budget_min=budget_min,
        budget_max=budget_max,
        budget_level=budget_level,
        travel_styles=travel_styles,
        special_needs=special_needs if special_needs else None,
        # 表单预选项一并落库，供后续步骤判断"是否已选过、免重复询问"
        transport_types=transport_types or [],
        food_preferences=food_preferences or [],
        accommodation_types=accommodation_types or [],
    )

    app_logger.info(
        f"需求预选项: auto_confirm={auto_confirm} "
        f"transport={transport_types} food={food_preferences} hotel={accommodation_types}"
    )

    # 返回 Command：更新状态并跳转到下一步
    return Command(update={
        "messages": [
            ToolMessage(
                content=f"需求已记录！\n"
                        f"出发日期：{departure_date}\n"
                        f"{travel_days} 天 | {adult_count + children_count} 人\n"
                        f"预算：{budget_min}-{budget_max} 元/人（{budget_level}级）\n"
                        f"风格：{', '.join(travel_styles)}",
                tool_call_id=runtime.tool_call_id
            )
        ],
        "user_requirement": requirement,
        # 确认模式真源写入状态：middleware 据此渲染 {{ auto_confirm_mode }}，
        # 各步骤 prompt 直接读模式变量，不再靠"清单里没有自动确认文案"反推手动模式
        "auto_confirm": auto_confirm,  # ★ 模式开关写进 state：后面的 middleware、编排层、一站式工具都读这一份
        "current_step": "destination_recommendation"  # 跳转到步骤2
    })
# ============== 2️.目的地选择工具 ==============

@tool
def select_destination_tool(
        destination: str,
        runtime: ToolRuntime[None, TravelState] = None
) -> Command:
    """
    确认用户选择的目的地，并转换到交通规划步骤。

    参数说明：
    - destination: 目的地名称。单目的地如 "西安"；多目的地按游览顺序用顿号连接，
      如 "杭州、深圳"（多城串联时**必须传完整城市串**，只传第一个城市会导致丢城）
    """

    app_logger.info(f"用户选择目的地: {destination}")

    return Command(update={
        "messages": [
            ToolMessage(
                content=f"目的地已确认：{destination}",
                tool_call_id=runtime.tool_call_id
            )
        ],
        "selected_destination": destination,
        "current_step": "transport_planning"  # 跳转到步骤3
    })


# ============== 5️.餐饮偏好选择工具 ==============

@tool
def select_food_tool(
        food_types: list[str],  # 可多选
        runtime: ToolRuntime[None, TravelState] = None
) -> Command:
    """
    确认用户选择的餐饮偏好（可多选），并转换到行程生成步骤。

    参数说明：
    - food_types: 餐饮类型列表，可选值：["specialty", "chain", "local"]
    """

    app_logger.info(f"用户选择餐饮类型: {food_types}")

    # 验证枚举值
    valid_types = {"specialty", "chain", "local"}
    if not all(t in valid_types for t in food_types):
        return Command(update={
            "messages": [
                ToolMessage(
                    content=f"❌ 餐饮类型无效，请从以下选择：{', '.join(valid_types)}",
                    tool_call_id=runtime.tool_call_id
                )
            ]
        })

    type_labels = {
        "specialty": "🍽️ 特色美食",
        "chain": "🍔 连锁快餐",
        "local": "🥘 本地小吃"
    }

    selected_labels = [type_labels[t] for t in food_types]

    return Command(update={
        "messages": [
            ToolMessage(
                content=f"餐饮偏好已确认：{', '.join(selected_labels)}",
                tool_call_id=runtime.tool_call_id
            )
        ],
        "selected_food_types": food_types,
        "current_step": "itinerary_generation"  # 跳转到步骤6
    })

# ============== 多目的地覆盖校验 ==============

def _multi_city_names(state) -> list[str]:
    """多目的地城市列表（按游览顺序）；单目的地返回空列表（调用方跳过校验）

    单城返回空列表是刻意设计：单目的地流程保持完全不变，不引入任何新校验
    """
    from app.services.express_planning_service import _split_cities
    cities = _split_cities(state.get("selected_destination") or "")
    return cities if len(cities) > 1 else []


def _expected_legs(state) -> list[str]:
    """应有的交通段：出发地→第一城 + 城际转场 + 末城→出发地返程（节点相邻城市不同才生成）。

 单目的地同样纳入段级校验（此前单城返回空列表跳过校验）。
 （用户规格）：按 _build_legs 口径——末城→出发地返程段一并计入，
    北京→上海 = 2 段、北京→天津→上海 = 3 段、北京→北京 = 0 段；出发地缺失时返回空列表
    """
    from app.services.express_planning_service import _build_legs, _split_cities
    departure = ((state.get("user_requirement") or {}).get("departure_city") or "").strip()
    if not departure:
        return []
    cities = _split_cities(state.get("selected_destination") or "")
    # ★ "应有几段"的唯一口径：交通步和第 12 章的执行器都认它
    return [f"{a}→{b}" for a, b in _build_legs(departure, cities)]


# 正文完整性守卫（`_itinerary_text_guard`）已按用户要求删除——
# 正文改由代码渲染后，模型自由正文一律作废重推，守卫已无可校验的对象。
# 此处仅保留天序号解析器：它仍被早期的"正文覆盖 1~N 天即推进状态机"兜底复用
# （那是流程推进保证，不是内容格式校验）
import re
_DAY_MARK_RE = re.compile(r"第\s*(\d+|[一二三四五六七八九十]+)\s*天|Day\s*(\d+)", re.IGNORECASE)
_CN_DIGIT = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _day_numbers(text: str) -> set[int]:
    """ 提取正文里出现过的天序号（第3天 / 第三天 / Day 3）"""
    nums: set[int] = set()
    for m in _DAY_MARK_RE.finditer(text or ""):
        if m.group(2):                                  # Day 4 / day 12
            nums.add(int(m.group(2)))
            continue
        g1 = m.group(1)                                 # 第3天 / 第1天 / 第三天
        if g1.isdigit():
            nums.add(int(g1))
            continue
        cn = g1
        if len(cn) == 1:
            nums.add(10 if cn == "十" else _CN_DIGIT.get(cn, 0))
        elif cn.startswith("十"):
            nums.add(10 + _CN_DIGIT.get(cn[1], 0))
        elif cn.endswith("十"):
            nums.add(_CN_DIGIT.get(cn[0], 0) * 10)
    return {n for n in nums if n > 0}


# ============== 段级循环：当前段的结构化选择 ==============


class LegChoice(BaseModel):
    """模型提交的"一段交通选择"。

    关键在于它是**一组字段**，不是一句话。早期实现里 legs 是 list[str]，每项形如
    "天津→石家庄 G6279 05:40-07:07 二等座105.5元"，于是代码要干三件苦差事：
      ① 判断"这条是不是当前待确认的那一段" → 拿起讫城市去做子串匹配；
      ② 取出车次号 → 上一条正则；
      ③ 取出票价给预算用 → 又一条正则。
    三件事全是"从一串自由文本里抠字段"，抠一次错一次（4.7 会看到子串匹配怎么把返程段
    认成去程段）。字段化之后：段靠字段相等匹配，车次直接读，票价直接进数字账本
    """

    from_city: str = Field(min_length=1, description='出发城市，如"北京"；必须与当前待确认段一致')
    to_city: str = Field(min_length=1, description='到达城市，如"天津"；必须与当前待确认段一致')
    vehicle_no: str = Field(default="", description='车次号或航班号，如"G1197"/"CA4105"；自驾留空')
    depart_time: str = Field(default="", description='出发时刻 HH:MM，如"05:40"')
    arrive_time: str = Field(default="", description='到达时刻 HH:MM，如"07:07"')
    seat_class: str = Field(default="", description='席别或舱位，如"二等座"')
    price: float = Field(default=0.0, ge=0, description="人均票价（元）；查不到填 0，**不要编造**")

    def summary(self) -> str:
        """段账本里的展示文本（给人看的那一份；数字另走 price 字段）"""
        span = f"{self.depart_time}-{self.arrive_time}".strip("-")
        fare = f"{self.seat_class}{self.price:g}元" if self.price > 0 else self.seat_class
        # ★ 空字段自动跳过：自驾什么都没有时退化成"已确认"，不会拼出一串空格
        return " ".join(p for p in (self.vehicle_no, span, fare) if p) or "已确认"


def _parse_leg_choices(legs) -> tuple[list["LegChoice"], list[str]]:
    """逐条过 schema；返回（合法条目, 中文错误说明）。坏的那条不连累好的那条"""
    picked: list[LegChoice] = []
    errors: list[str] = []
    for index, raw in enumerate(legs or [], start=1):
        try:
            picked.append(LegChoice.model_validate(raw))
        except ValidationError as exc:
            detail = "；".join(
                f"{'.'.join(str(x) for x in item['loc'])} → {item['msg']}"
                for item in exc.errors()[:3]
            )
            errors.append(f"第 {index} 条：{detail}")
    return picked, errors


def _match_leg(picked: list["LegChoice"], pending: str) -> "LegChoice | None":
    """按**字段相等**找出当前待确认段的那一条（不做任何子串匹配）"""
    frm, _, to = pending.partition("→")
    for choice in picked:
        # ★ 相等，不是包含：'天津→北京' 不会被当成 '北京→天津'
        if choice.from_city.strip() == frm.strip() and choice.to_city.strip() == to.strip():
            return choice
    return None

# ============== 3️.交通方式选择工具 ==============

@tool
def select_transport_tool(
        transport_type: str,  # "flight" | "train" | "driving"
        legs: Optional[list[LegChoice]] = None,
        runtime: ToolRuntime[None, TravelState] = None
) -> Command:
    """
    确认用户选择的交通方式，并转换到住宿规划步骤。

    参数说明：
    - transport_type: 交通方式，可选值：flight（航班）、train（高铁）、driving（自驾）
    - legs: **多目的地必传**（单目的地可省略）：只放**当前正在确认的那一段**，
      每段写成一个对象（不是一句话），如
      [{"from_city": "天津", "to_city": "石家庄", "vehicle_no": "G6279",
        "depart_time": "05:40", "arrive_time": "07:07",
        "seat_class": "二等座", "price": 105.5}]
      `from_city` / `to_city` 必填，且必须与当前待确认段完全一致，传错段会被拒绝并
      告知当前应确认哪一段；`price` 填用户所选班次的真实人均票价，查不到就填 0，
      **不要编造**；全部段确认完才会推进到住宿步骤
    """

    # 埋点：旧交通选择循环（逐段/整步）仍在被调用——观测是否还有真实使用者，
    # 零命中后才可安全删除旧循环（只记日志，零副作用）
    app_logger.info(f"[LEGACY] select_transport_tool 被调用：type={transport_type} legs={legs}")
    # 验证枚举值
    if transport_type not in ["flight", "train", "driving"]:
        return Command(update={
            "messages": [
                ToolMessage(
                    content="❌交通方式无效，请选择：flight、train 或 driving",
                    tool_call_id=runtime.tool_call_id
                )
            ]
        })

    # 多目的地 = 段级循环——一次只确认一段，未确认完所有段（含返程）
    # 由代码阻止推进。单目的地 expected 为空，走下面的"整步确认"老路径，行为完全不变。
    # 为什么：旧实现要求模型一次传全部段，实测它做不到——用户确认首段后模型不知还剩几段，
    # 于是编造 G1234/G2001/G1201 并漏掉返程段；流程账本不该交给模型，改由游标确定性维护
    # ★ 先过 schema：模型提交的是**结构化对象**，不是一句话。
    #   坏的那条不连累好的那条；一条都不合规时，errors 会被写进下面的报错文案里
    picked, errors = _parse_leg_choices(legs)

    # 多目的地 = 段级循环——一次只确认一段，未确认完所有段
    # 由代码阻止推进。单目的地 expected 为空，走下面的"整步确认"老路径，行为完全不变。
    # 为什么：旧实现要求模型一次传全部段，实测它做不到——用户确认首段后模型不知还剩几段，
    # 于是编造 G1234/G2001/G1201 并漏掉返程段；流程账本不该交给模型，改由游标确定性维护
    expected = _expected_legs(runtime.state)
    leg_texts = [f"{c.from_city}→{c.to_city} {c.summary()}" for c in picked]
    leg_state: dict = {}
    if expected:
        recorded = dict(runtime.state.get("leg_selections") or {})
        prices = dict(runtime.state.get("leg_prices") or {})
        cursor = max(0, min(int(runtime.state.get("leg_cursor") or 0), len(expected) - 1))
        # ★ 流程账本归代码：现在该确认第几段，由 leg_cursor 说了算
        pending = expected[cursor]
        hit = _match_leg(picked, pending)
        if hit is None:
            # 早期实现在这里还要抢救一把：从 legs 文本、再从用户原话里用正则捞车次号回填。
            # 现在不捞了——与其猜"用户那句话里哪串字符是车次"，不如把**该交什么**原样告诉模型。
            # 报错文案是写给模型看的（环节 6.4）：写清段名、字段名和样例，它下一轮就能改对
            app_logger.info(f"[guard] 段级循环：待确认段 {pending} 未出现在 legs={legs}")
            _frm, _, _to = pending.partition("→")
            return Command(update={"messages": [ToolMessage(
                content=(f"❌ 当前待确认的是第 {cursor + 1}/{len(expected)} 段「{pending}」。"
                         + (f"提交的 legs 有格式问题：{'；'.join(errors)}。" if errors
                            else "本次 legs 里没有这一段。")
                         + f"请先查询并展示「{pending}」的班次，等用户挑选后，把该段写成对象传进 legs："
                           f'{{"from_city": "{_frm}", "to_city": "{_to}", "vehicle_no": "车次号", '
                           f'"depart_time": "HH:MM", "arrive_time": "HH:MM", '
                           f'"seat_class": "席别", "price": 票价数字}}'
                           f"（其余段不要提前展示）"),
                tool_call_id=runtime.tool_call_id)]})
        # 展示文本由 summary() 从字段拼出来，天然不含段名，
        # 所以早期那段"裁掉与段名重复的前缀"的去重逻辑一并消失了
        shown = hit.summary()
        recorded[pending] = shown
        if hit.price > 0:
            # ★ 数字账本（第 9 章的 leg_prices）：预算工具直接读这一份，不去解析上面那串展示文本
            prices[pending] = float(hit.price)
        if cursor + 1 < len(expected):
            nxt = expected[cursor + 1]
            app_logger.info(
                f"[legs] 第 {cursor + 1}/{len(expected)} 段已确认 {pending}｜{shown}，待确认 {nxt}"
            )
            return Command(update={
                "messages": [ToolMessage(
                    content=(f"✅ 第 {cursor + 1}/{len(expected)} 段已确认：{pending}｜{shown}\n"
                             f"请立即查询并展示下一段「{nxt}」的班次供用户挑选"
                             f"（只展示这一段，禁止提前展示其它段，也不要再说已确认该段）"),
                    tool_call_id=runtime.tool_call_id)],
                "leg_selections": recorded,
                "leg_prices": prices,
                "leg_cursor": cursor + 1,
            })
        # 全部段确认完毕：整理成清单，与 current_step 一次性原子写入（避免并发写冲突）
        leg_texts = [f"{k} {v}" for k, v in recorded.items()]
        leg_state = {"leg_selections": recorded, "leg_prices": prices, "leg_cursor": cursor + 1}

    transport_labels = {
        "flight": "航班",
        "train": "高铁",
        "driving": "自驾"
    }

    return Command(update={
        "messages": [
            ToolMessage(
                content=f"交通方式已确认：{transport_labels[transport_type]}",
                tool_call_id=runtime.tool_call_id
            )
        ],
        "selected_transport": transport_type,
        # 逐段确认结果留痕（单城为空列表），行程步骤按此安排城际转运
        "selected_transport_legs": leg_texts,
        "current_step": "accommodation_planning",  # 跳转到步骤4
        **leg_state,
    })

# ============== 4️.住宿偏好选择工具 ==============

@tool
def select_accommodation_tool(
        accommodation_types: list[str],  # 可多选
        hotels: Optional[list[str]] = None,
        runtime: ToolRuntime[None, TravelState] = None
) -> Command:
    """
    确认用户选择的住宿偏好（可多选），并转换到餐饮规划步骤。

    参数说明：
    - accommodation_types: 住宿类型列表，可选值：
      ["star_hotel", "economy_hotel", "hostel", "youth_hostel"]
    - hotels: **多目的地必传**（单目的地可省略）：只放**当前正在确认的那一城**，如
      ["杭州：杭州XX酒店 520元/晚"]。系统按城市游标逐城要求确认，传错城市会被拒绝并告知
      当前应确认哪一城；所有城市都确认完才会推进到餐饮步骤
    """

    # [LEGACY] 埋点：旧住宿逐城/整步循环仍在被调用——观测是否还有真实使用者，
    # 零命中后才可安全删除（只记日志，零副作用）
    app_logger.info(f"[LEGACY] select_accommodation_tool 被调用：{accommodation_types} hotels={hotels}")

    # 验证枚举值
    valid_types = {"star_hotel", "economy_hotel", "hostel", "youth_hostel"}
    if not all(t in valid_types for t in accommodation_types):
        return Command(update={
            "messages": [
                ToolMessage(
                    content=f"❌住宿类型无效，请从以下选择：{', '.join(valid_types)}",
                    tool_call_id=runtime.tool_call_id
                )
            ]
        })

    type_labels = {
        "star_hotel": "星级酒店",
        "economy_hotel": "经济酒店",
        "hostel": "特色民宿",
        "youth_hostel": "青年旅社"
    }

    selected_labels = [type_labels[t] for t in accommodation_types]

    # 多目的地住宿同样改为"逐城循环"——一次只确认一城，与交通段级循环对称。
    # 单目的地 cities 为空 → 走下面的整步确认老路径，行为完全不变
    cities = _multi_city_names(runtime.state)
    hotel_texts = list(hotels or [])
    stay_state: dict = {}
    # ★ 数字账本：从酒店文本（"杭州：XX酒店 520元/晚"）抽真实房价写入 stay_prices，
    #   否则预算工具读不到、只能按 300 元/晚估算（卡片路线已由 hotel_flow 写，这里补非卡片路线）
    from app.services.express_planning_service import _number_before, _split_cities
    stay_prices = dict(runtime.state.get("stay_prices") or {})
    if cities:
        recorded = list(runtime.state.get("selected_hotels") or [])
        cursor = max(0, min(int(runtime.state.get("stay_cursor") or 0), len(cities) - 1))
        # ★ 逐城循环与段级循环对称：游标换成 stay_cursor
        pending = cities[cursor]
        hit = next((g for g in (hotels or []) if pending in g), None)
        if not hit:
            app_logger.info(f"[guard] 逐城循环：待确认城市 {pending} 未出现在 hotels={hotels}")
            return Command(update={"messages": [ToolMessage(
                content=(f"❌ 当前待确认的是第 {cursor + 1}/{len(cities)} 个城市「{pending}」，"
                         f"但本次 hotels 里没有该城市。请先查询并展示「{pending}」的酒店候选，"
                         f"等用户挑选后再把该城写进 hotels 调用本工具（其余城市不要提前展示）"),
                tool_call_id=runtime.tool_call_id)]})
        if not any(pending in r for r in recorded):
            recorded.append(hit)
        _price = _number_before(hit, "元/晚")
        if _price > 0:
            stay_prices[pending] = float(_price)
        if cursor + 1 < len(cities):
            nxt = cities[cursor + 1]
            app_logger.info(
                f"[stays] 第 {cursor + 1}/{len(cities)} 城已确认 {pending}，待确认 {nxt}"
            )
            return Command(update={
                "messages": [ToolMessage(
                    content=(f"✅ 第 {cursor + 1}/{len(cities)} 城住宿已确认：{pending}｜{hit}\n"
                             f"请立即查询并展示下一城「{nxt}」的酒店候选供用户挑选"
                             f"（只展示这一城，禁止提前展示其它城市）"),
                    tool_call_id=runtime.tool_call_id)],
                "selected_hotels": recorded,
                "stay_cursor": cursor + 1,
                "stay_prices": stay_prices,
            })
        hotel_texts = recorded
        stay_state = {"selected_hotels": recorded, "stay_cursor": cursor + 1, "stay_prices": stay_prices}
    else:
        # ★ 单城非卡片路线此前只记展示文本、不落数字账本 → 预算回退 300元/晚。
        #   与多城分支同口径：键取单体城市名（_split_cities 的单元素），而非原始 destination 串
        _single = next(iter(_split_cities(runtime.state.get("selected_destination") or "")), "")
        _price = _number_before(hotel_texts[0], "元/晚") if hotel_texts else 0
        if _single and _price > 0:
            stay_prices[_single] = float(_price)
            stay_state = {"stay_prices": stay_prices}

    return Command(update={
        "messages": [
            ToolMessage(
                content=f"住宿偏好已确认：{', '.join(selected_labels)}",
                tool_call_id=runtime.tool_call_id
            )
        ],
        "selected_accommodation_types": accommodation_types,
        # 逐城确认结果留痕（单城为空列表），行程步骤按此安排住宿
        "selected_hotels": hotel_texts,
        "current_step": "food_planning",  # 跳转到步骤5
        **stay_state,
    })

# ============== 5️.住宿+餐饮一站式确认工具 ==============

@tool
def select_accommodation_food_tool(
        accommodation_types: list[str],  # 可多选
        food_types: list[str],  # 可多选
        runtime: ToolRuntime[None, TravelState] = None
) -> Command:
    """
    一站式确认住宿与餐饮偏好，直接推进到行程生成步骤（AI 自动确认场景专用）。

    为什么需要：住宿轮与餐饮轮同轮分别调用两个 select 工具时，两个 Command 并行
    写 current_step 会触发 LangGraph InvalidUpdateError（单值通道并发写入冲突），
    因此合并为一次原子调用，只写一次 current_step。

    参数说明：
    - accommodation_types: 住宿类型列表，可选值：
      ["star_hotel", "economy_hotel", "hostel", "youth_hostel"]
    - food_types: 餐饮类型列表，可选值：["specialty", "chain", "local"]
    """
    # 埋点：旧"住宿+餐饮一站式"推进器仍在被调用（仅自动确认模式使用）——
    # 观测是否还有真实使用者，零命中后才可安全删除（只记日志，零副作用）
    app_logger.info(f"[LEGACY] select_accommodation_food_tool 被调用：{accommodation_types} / {food_types}")

    if not runtime.state.get("auto_confirm"):
        app_logger.info("[guard] 手动模式越权调用 select_accommodation_food_tool，已拒绝")
        return Command(update={"messages": [ToolMessage(
            content=("❌ 当前为手动确认模式，禁止用一站式工具跳过用户确认。"
                     "请按住宿→餐饮依次调用 select_accommodation_tool / select_food_tool，"
                     "且每次调用前先展示候选并等用户回复确认。"),
            tool_call_id=runtime.tool_call_id)]})

    # 校验住宿枚举
    valid_accom = {"star_hotel", "economy_hotel", "hostel", "youth_hostel"}
    if not all(t in valid_accom for t in accommodation_types):
        return Command(update={"messages": [ToolMessage(
            content=f"住宿类型无效，请从以下选择：{', '.join(valid_accom)}",
            tool_call_id=runtime.tool_call_id)]})

    # 校验餐饮枚举
    valid_food = {"specialty", "chain", "local"}
    if not all(t in valid_food for t in food_types):
        return Command(update={"messages": [ToolMessage(
            content=f"餐饮类型无效，请从以下选择：{', '.join(valid_food)}",
            tool_call_id=runtime.tool_call_id)]})

    # 标签映射（与单独工具保持一致）
    accom_labels = {
        "star_hotel": "星级酒店", "economy_hotel": "经济酒店",
        "hostel": "特色民宿", "youth_hostel": "青年旅社"
    }
    food_labels = {
        "specialty": "🍽️ 特色美食", "chain": "🍔 连锁快餐", "local": "🥘 本地小吃"
    }

    # 单次 Command 原子写入：两个状态 + 一次 current_step 推进
    return Command(update={
        "messages": [ToolMessage(
            content=(f"住宿偏好已确认：{', '.join(accom_labels[t] for t in accommodation_types)}；"
                     f"餐饮偏好已确认：{', '.join(food_labels[t] for t in food_types)}"),
            tool_call_id=runtime.tool_call_id)],
        "selected_accommodation_types": accommodation_types,
        "selected_food_types": food_types,
        "current_step": "itinerary_generation"  # 直达步骤6（跳过餐饮轮）
    })

# ============== 6️.行程生成工具 ==============

def _multi_city_day_floor_error(state) -> str | None:
    """多目的地天数硬约束（代码层，非提示词）：天数 < 目的地数×2 时返回拒绝文案，否则 None。

    单一实现、两处入口共用（generate_itinerary_tool 与 submit_itinerary_body_tool），
    避免同一条下限公式各写一份后漂移；口径与快速通道同源（每个目的地 2 天）
    """
    _req = state.get("user_requirement") or {}
    from app.services.express_planning_service import _split_cities
    cities = _split_cities(_req.get("destination") or "")
    need = len(cities) * 2
    if len(cities) > 1 and int(_req.get("travel_days") or 0) < need:
        return (f"❌ 您选择了 {len(cities)} 个目的地，至少需要 {need} 天（目的地数 × 2），"
                f"当前需求无法生成完整报告。请将旅行天数调整为 {need} 天及以上，或减少目的地。")
    return None

# ============== 6️⃣ 行程生成工具（骨架版） ==============

@tool
def generate_itinerary_tool(
        runtime: ToolRuntime[None, TravelState] = None
) -> Command:
    """
    生成完整行程安排，并转换到预算汇总步骤。

    此工具会综合：
    - 用户需求（天数、人数、风格）
    - 目的地信息
    - 交通信息
    - 住宿信息
    - 餐饮信息

    生成详细的每日行程。
    """

    app_logger.info("开始生成行程...")

    state = runtime.state

    # 检查必要信息是否完整
    required_fields = [
        # 用户的基本需求
        "user_requirement",
        # 目的地选择
        "selected_destination",
        # 交通选择
        "selected_transport",
        # 住宿选择
        "selected_accommodation_types",
        # 食物选择
        "selected_food_types"
    ]

    missing = [f for f in required_fields if f not in state or state[f] is None]
    if missing:
        return Command(update={
            "messages": [
                ToolMessage(
                    content=f"❌ 信息不完整，缺少：{', '.join(missing)}",
                    tool_call_id=runtime.tool_call_id
                )
            ]
        })

    # 多目的地天数硬约束（代码层，非提示词）：不推进 current_step，下游清单门禁随之失效，
    # 即使用户坚持也只停在状态机里、不产出报告——必须补足天数或减少目的地才放行
    # 下限公式与 submit_itinerary_body_tool 共用同一实现，避免两份口径漂移
    _floor_err = _multi_city_day_floor_error(state)
    if _floor_err:
        app_logger.info(f"[guard] 多目的地天数不足，拒绝生成行程：{_floor_err}")
        return Command(update={
            "messages": [ToolMessage(content=_floor_err, tool_call_id=runtime.tool_call_id)]
        })

    # 生成行程（简化版，实际应调用 LLM）
    travel_days = state["user_requirement"]["travel_days"]
    itinerary = []

    for day in range(1, travel_days + 1):
        itinerary.append({
            "day_number": day,
            "activities": [f"第{day}天活动1", f"第{day}天活动2"],
            "meals": ["早餐", "午餐", "晚餐"],
            "accommodation": "酒店名称"
        })

    return Command(update={
        "messages": [
            ToolMessage(
                content=f"已生成 {travel_days} 天详细行程！",
                tool_call_id=runtime.tool_call_id
            )
        ],
        "itinerary": itinerary,
        "current_step": "budget_summarization"  # 跳转到步骤7
    })

# ============== 行程正文结构化（Pydantic） ==============
_SLOT_KEYS = ("morning", "noon", "afternoon", "evening")
_SLOT_LABELS = {"morning": "上午", "noon": "中午", "afternoon": "下午", "evening": "晚上"}
_RETURN_HINTS = ("返程", "返回", "回程")
_ITEM_MIN_CHARS = 15

PlanItem = Annotated[str, Field(min_length=1)]

class DayPlan(BaseModel):
    """单日行程：分4个时段，配套当日小贴士"""
    day: int = Field(ge=1, description="第几天（从1开始，连续覆盖1~旅行天数）")
    city: str = Field(default="", description="当天所在城市，如“重庆”")
    morning: list[PlanItem] = Field(min_length=1, description="上午安排（1~2条，含玩法/看点/门票或耗时）")
    noon: list[PlanItem] = Field(min_length=1, description="中午安排（1~2条，午餐写店名+招牌菜+人均）")
    afternoon: list[PlanItem] = Field(min_length=1, description="下午安排（1~2条，含玩法与建议停留时长）")
    evening: list[PlanItem] = Field(min_length=1, description="晚上安排（1~2条；末日晚上必须含返程）")
    tips: list[PlanItem] = Field(default_factory=list, description="当天实用小贴士（1~3条：门票/预约/交通接驳/避坑）")


class ItineraryBody(BaseModel):
    """完整行程结构化模型，校验天数连续性、条目长度、末日返程规则"""
    days: list[DayPlan] = Field(min_length=1, description="逐日行程列表，按天序号升序")

    @model_validator(mode="after")
    def _check_days(self, info: ValidationInfo) -> "ItineraryBody":
        nums = [d.day for d in self.days]
        if len(set(nums)) != len(nums):
            raise ValueError(f"天序号重复：{sorted(nums)}，每天只能出现一次")

        need = (info.context or {}).get("travel_days") or 0

        for d in self.days:
            if not d.tips:
                raise ValueError(f"第 {d.day} 天缺少 tips：每天至少 1 条实用小贴士（预约/门票/交通接驳/避坑）")
            for key in _SLOT_KEYS + ("tips",):
                for item in getattr(d, key):
                    if len(str(item).strip()) >= _ITEM_MIN_CHARS:
                        continue
                    raise ValueError(
                        f"第 {d.day} 天「{_SLOT_LABELS.get(key, '小贴士')}」的条目「{str(item).strip()}」"
                        f"只有 {len(str(item).strip())} 字：每条不少于 {_ITEM_MIN_CHARS} 字"
                        f"（时间/地点 + 玩法或看点 + 价格或时长）"
                    )

        if not need:
            return self

        miss = [d for d in range(1, need + 1) if d not in nums]
        if miss:
            raise ValueError(f"缺少第 {'、'.join(str(d) for d in miss)} 天：必须恰好覆盖第 1~{need} 天")
        if max(nums) > need:
            raise ValueError(f"超出旅行天数：全程仅 {need} 天，却出现第 {max(nums)} 天")

        tail = next((d for d in self.days if d.day == need), None)
        if tail and not any(h in " ".join(tail.evening) for h in _RETURN_HINTS):
            raise ValueError(f"第 {need} 天（末日）晚上必须包含返程安排，如“乘 G1710 返回天津”")
        return self


# 行程结构规范化兜底：以表单旅行天数为准，自动修复结构缺陷
_ITINERARY_FILL: dict[str, tuple[str, ...]] = {
    "morning": (
        "{city}市区漫步或就近景点游览（09:00 出发），按体力灵活安排，建议停留 1~2 小时，多数免费开放",
        "上午到{city}人气街区与公园慢逛（09:30 前后出门），以拍照歇脚为主，用时约 2 小时，花费可控",
    ),
    "noon": (
        "{city}就近选一家本地口碑餐馆用餐（12:00 前后），人均约 50 元，避开景点门口高价店，饭后稍作休息",
        "午餐在{city}商圈内解决（12:30 前后），优先本地小吃组合，人均约 40 元，吃完原地休息半小时",
    ),
    "afternoon": (
        "{city}街区闲逛或咖啡馆休整（14:00 开始），可挑周边小景点补充参观，建议停留 2 小时，多数无需门票",
        "下午安排{city}特色展馆或水岸步道（14:30 前后），以免费预约场馆为主，用时约 2 小时，注意闭馆时间",
    ),
    "evening": (
        "{city}就近晚餐后返回住处休息（18:00 前后），人均约 60 元，留意末班地铁与公交时间",
        "晚上在{city}夜市或步行街觅食（18:30 前后），人均约 50 元，饭后早点回住处，方便次日出行",
    ),
}

def _fill_item(key: str, day: int, city: str) -> str:
    """空缺时段补位条目，按天轮换文案并填充对应城市，避免多天重复"""
    variants = _ITINERARY_FILL.get(key) or ("{city}自由活动，按现场情况灵活安排，注意留出交通时间",)
    return variants[(max(1, day) - 1) % len(variants)].replace("{city}", city or "当地")


_FILL_TIPS: tuple[str, ...] = (
    "提前查询景点开放时间与预约要求，避免现场排队；市内出行优先地铁，避开早晚高峰",
    "当天行程较松，可临时加一处顺路小景点；随身带好身份证与充电宝，注意天气变化",
)

def _fill_tip(day: int, city: str) -> str:
    """空缺小贴士补位条目，按天轮换文案，避免多天重复"""
    return f"{city or '当地'}：{_FILL_TIPS[(max(1, day) - 1) % len(_FILL_TIPS)].strip()}"


def _day_city_map(state) -> list[str]:
    """生成逐日城市序列，与住宿、停留晚数同源"""
    from app.services.express_planning_service import ordered_play_cities, _stay_nights
    req = state.get("user_requirement") or {}
    days = int(req.get("travel_days") or 0)
    nodes = ordered_play_cities(
        str(req.get("departure_city") or ""),
        str(state.get("selected_destination") or "") or str(req.get("destination") or ""),
    )
    if not nodes or days <= 0:
        return []
    out: list[str] = []
    for node, night in zip(nodes, _stay_nights(days, len(nodes))):
        out.extend([node] * night)
    return out


def _fill_day_cities(days: list, cities: list[str]) -> list:
    """为缺失城市字段的每日行程补全城市名称"""
    out = []
    for raw in days or []:
        if not isinstance(raw, dict) or str(raw.get("city") or "").strip():
            out.append(raw)
            continue
        try:
            day = int(raw.get("day") or 0)
        except (TypeError, ValueError):
            day = 0
        out.append({**raw, "city": cities[day - 1]} if 0 < day <= len(cities) else raw)
    return out


_ITEM_PAD = "（建议停留 1~2 小时，按现场情况调整）"
_BACK_PAD = "，提前 1 小时到站取票安检，按已确认班次乘车返程"
_TIP_PAD = "，请提前在官方渠道核实预约要求与开放时间"

def _back_hint(state) -> str:
    """生成末日晚上的返程兜底文案，优先读取已确认返程班次"""
    if state.get("selected_transport") == "local":
        return "傍晚结束本次本地行程，返回住处整理行李，按需预约次日送站/送机"
    city = ((state.get("user_requirement") or {}).get("departure_city")) or ""
    back = next((str(v) for k, v in reversed(list((state.get("leg_selections") or {}).items()))
                 if v and city and str(k).endswith(f"→{city}")), "")
    if back:
        return f"按已确认班次返程（{back}）"
    return f"傍晚返程回到{city}" if city else ""


def _normalize_itinerary_days(
    days: list[dict], need: int, back_text: str = "", cities: list[str] | None = None
) -> tuple[list[dict], list[str]]:
    """按旅行天数归一化逐日结构：只补空缺、只裁非法，不改写模型已给出的有效内容

 补位条目按天轮换 + 写入当天城市（cities 由 _day_city_map 供），
    并给漏填 city 的天补上城市标注（此前 Day5/Day6 无城市标注、多天出现同一条补位文案）
    """
    fixes: list[str] = []
    kept: dict[int, dict] = {}
    for raw in days or []:
        if not isinstance(raw, dict):
            continue
        try:
            day = int(raw.get("day") or 0)
        except (TypeError, ValueError):
            continue
        if day < 1 or (need and day > need) or day in kept:
            fixes.append(f"忽略第 {day} 天（越界或重复）")
            continue
        kept[day] = raw
    total = need or (max(kept) if kept else 1)
    out: list[dict] = []
    for day in range(1, total + 1):
        d = dict(kept.get(day) or {})
        d["day"] = day
        if not str(d.get("city") or "").strip() and cities and 0 < day <= len(cities):
            d["city"] = cities[day - 1]
            fixes.append(f"补第 {day} 天城市（{cities[day - 1]}）")
        if day not in kept:
            fixes.append(f"补第 {day} 天")
        last = bool(need) and day == need
        for key in _SLOT_KEYS:
            items = [str(x).strip() for x in (d.get(key) or []) if str(x).strip()]
            items = [x if len(x) >= _ITEM_MIN_CHARS else x + _ITEM_PAD for x in items]
            # 只在**空时段**补位（floor=1）——模型已给出内容的时段原样保留，
            # 不再为凑"≥2 条"追加通用句（同槽两条并列会被读成重复安排）
            floor = 1  # ★ 只给空时段补位：模型已给内容的时段原样保留
            while len(items) < floor:
                items.append(_fill_item(key, day, str(d.get("city") or "")))
                fixes.append(f"补第 {day} 天{_SLOT_LABELS[key]}")
            d[key] = items
        tips = [str(x).strip() for x in (d.get("tips") or []) if str(x).strip()]
        tips = [x if len(x) >= _ITEM_MIN_CHARS else x + _TIP_PAD for x in tips]
        d["tips"] = tips or [_fill_tip(day, str(d.get("city") or ""))]
        if not tips:
            fixes.append(f"补第 {day} 天小贴士")
        if last and not any(h in " ".join(d["evening"]) for h in _RETURN_HINTS):
            back = back_text or "傍晚按已确认的交通班次返程"
            if len(back) < _ITEM_MIN_CHARS:  # 短句会被长度校验拦下，代码补足
                back += _BACK_PAD
            d["evening"] = d["evening"] + [back]
            fixes.append(f"补第 {day} 天返程")
        out.append(d)
    return out, fixes

@tool
def submit_itinerary_body_tool(
        days: list[dict],
        runtime: ToolRuntime[None, TravelState] = None
) -> Command:
    """提交结构化逐日行程；校验通过后由系统渲染正文与预算表。
    仅提交数据，禁止自行编写行程正文或预算表格，正文、预算明细表由系统按结构渲染。

    参数说明：
    - days: 逐日行程列表，单条样例
      {"day": 1, "city": "北京",
       "morning": ["09:00 天安门广场观升旗（提前1小时到，安检排队约20分钟；广场免费，故宫需提前7天预约）"],
       "noon": ["12:00 前门大街「四季民福」烤鸭，招牌酥皮烤鸭+芥末鸭掌，人均约150元"],
       "afternoon": ["14:00 故宫博物院中轴线游览（午门进、神武门出，重点看太和殿与珍宝馆，建议停留3小时，门票60元）"],
       "evening": ["18:00 什刹海「烤肉季」铜锅涮肉，人均约120元，2人铜锅+麻酱烧饼"],
       "tips": ["故宫、国博需实名预约，建议出发前7天在官方小程序抢票", "地铁1号线天安门东站出站最便捷，天安门周边不可停车"]}
    硬约束：每个时段至少1条，每条不少于15字（建议写到25~60字）；每天至少1条tips；天序号覆盖1~旅行天数、不重不漏；最后一天晚上必须包含返程。
    """
    state = runtime.state
    # 多目的地天数硬约束：与 generate_itinerary_tool 同源（结构化提交入口此前漏检——
    # 正文改走本工具时，天数不足的多城需求会被放行、产出不完整报告）
    _floor_err = _multi_city_day_floor_error(state)
    if _floor_err:
        app_logger.info(f"[guard] 多目的地天数不足，拒绝提交行程结构：{_floor_err}")
        return Command(update={
            "messages": [ToolMessage(content=_floor_err, tool_call_id=runtime.tool_call_id)]
        })
    need = int(((state.get("user_requirement") or {}).get("travel_days")) or 0)

    auto_budget: dict = {}
    if not (state.get("budget") or {}).get("total"):
        _raw = getattr(summarize_budget_tool, "func", None)
        if _raw is not None:
            extra = dict(_raw(runtime=runtime).update or {})
            extra.pop("messages", None)
            auto_budget = extra
            auto_budget.pop("user_requirement", None)
            app_logger.info("预算缺失（与预算工具同轮并行调用）：已由行程提交工具就地补算并同批落库")

    if not (auto_budget.get("budget") or state.get("budget") or {}).get("total"):
        app_logger.warning("🚫 提交行程结构被拒：预算缺失且就地补算未成功")
        return Command(update={"messages": [ToolMessage(
            content=("❌ 预算尚未计算，无法渲染预算明细表。请先调用 `summarize_budget_tool`"
                     "汇总预算，再调用本工具提交行程结构（顺序：预算 → 行程结构）。"),
            tool_call_id=runtime.tool_call_id)]})

    _cities = _day_city_map(state)
    days = _fill_day_cities(days, _cities)

    try:
        body = ItineraryBody.model_validate({"days": days}, context={"travel_days": need})
    except ValidationError as e:
        err = "；".join(f"{'.'.join(str(x) for x in it['loc'])} → {it['msg']}" for it in e.errors()[:5])
        app_logger.warning(f"⚠️ 行程结构未过校验，转由代码规范化（{need} 天）：{err}")
        fixed, fixes = _normalize_itinerary_days(
            days, need, back_text=_back_hint(state), cities=_cities
        )
        try:
            body = ItineraryBody.model_validate({"days": fixed}, context={"travel_days": need})
        except ValidationError as e2:
            err2 = "；".join(f"{'.'.join(str(x) for x in it['loc'])} → {it['msg']}" for it in e2.errors()[:5])
            app_logger.warning(f"❌ 规范化后仍未通过（不应发生）：{err2}")
            return Command(update={"messages": [ToolMessage(
                content=(f"行程结构校验失败（全程 {need} 天）：{err2}\n"
                         f"请修正后重新调用本工具提交完整结构（只提交数据，不要输出正文）"),
                tool_call_id=runtime.tool_call_id)]})
        app_logger.warning(f"⚠️ 已由代码规范化 {len(fixes)} 处：{'；'.join(fixes[:6])}")
        fixes_note = f"（系统已按 {need} 天规范化 {len(fixes)} 处空缺）" if fixes else ""
    else:
        fixes_note = ""

    dumped = [d.model_dump() for d in body.days]
    app_logger.info(f"✅ 行程结构就绪：{len(dumped)} 天（末日含返程）{fixes_note}，正文交由代码渲染")
    return Command(update={
        "messages": [ToolMessage(
            content=(f"✅ 已接收 {len(dumped)} 天行程结构{fixes_note}。正文与预算表由系统渲染，"
                     f"本轮不要再输出任何行程正文、时段列表或预算表格，只回一句邀请语："
                     f"「如需调整（天数/预算/目的地等），直接告诉我即可。」"),
            tool_call_id=runtime.tool_call_id)],
        **auto_budget,
        "itinerary_body": dumped,
        "itinerary": [{"day_number": d["day"]} for d in dumped],
        "current_step": "budget_summarization",
    })
# ============== 预算汇总工具 ==============
@tool
def summarize_budget_tool(
        attraction_tickets: list[dict] = None,
        transport_price_per_person: float = None,
        hotel_price_per_night: float = None,
        hotel_rooms: int = None,
        travel_days: int = None,
        budget_max: float = None,
        runtime: ToolRuntime[None, TravelState] = None
) -> Command:
    """
    汇总各项费用，生成预算明细。订单环节已移除，预算汇总后流程即完成。
    Args:
        attraction_tickets: 真实门票列表，格式 [{"name": "景点名", "price": 单人票价(元)}]；
                            提取不到真实票价时可省略，门票项回退人均估算法。
        transport_price_per_person: 真实人均交通价（元，取自已选定班次）；
                                    省略时回退 500 元/人估算。
        hotel_price_per_night: 真实每晚房价（元，取自已选定酒店）；
                               省略时回退 300 元/晚估算。
        hotel_rooms: 房间数；省略时按每间2人自动折算（ceil(总人数/2)）。
        travel_days: 覆盖天数；用户调整行程天数时必传，省略则使用状态机记录的travel_days。
        budget_max: 覆盖人均预算上限；用户调整预算时必传，省略则使用状态机记录的budget_max。
    预算明细包含：交通费用、住宿费用、餐饮费用、景点门票、其他杂费
    """
    app_logger.info("开始计算预算...")
    state = runtime.state

    def _legs_price_per_person() -> float | None:
        """各已确认段票价之和（人均一趟）；账本为空返回 None"""
        booked = [float(v) for v in (state.get("leg_prices") or {}).values() if float(v) > 0]
        return sum(booked) if booked else None

    def _hotel_price_per_night_from_book() -> float | None:
        """各城已确认酒店元/晚均价；账本为空返回 None"""
        booked = [float(v) for v in (state.get("stay_prices") or {}).values() if float(v) > 0]
        return (sum(booked) / len(booked)) if booked else None

    requirement = state["user_requirement"]
    total_people = requirement["adult_count"] + requirement["children_count"]

    travel_days = (travel_days if isinstance(travel_days, int) and travel_days > 0
                   else requirement["travel_days"])

    import math
    nights = max(travel_days - 1, 1)
    rooms = hotel_rooms if isinstance(hotel_rooms, int) and hotel_rooms > 0 \
        else max(math.ceil(total_people / 2), 1)

    # 三级优先级：入参传入值 -> 账本真实价格 -> 估算兜底
    _transport_pp = (transport_price_per_person
                     if isinstance(transport_price_per_person, (int, float)) and transport_price_per_person > 0
                     else _legs_price_per_person())
    _hotel_pn = (hotel_price_per_night
                 if isinstance(hotel_price_per_night, (int, float)) and hotel_price_per_night > 0
                 else _hotel_price_per_night_from_book())

    # ★ 无真实票价时的兜底必须随段数缩放：与快速通道 _build_budget 同口径（500 元/人/段）。
    #   _legs_price_per_person() 已是"全段人均之和"，兜底若仍按 500 元/人（与段数无关），
    #   多段行程会被严重低估；段数用 _expected_legs 计（含末城→出发地返程段）
    total_legs = len(_expected_legs(state))
    transport_cost = (_transport_pp if _transport_pp and _transport_pp > 0
                      else 500 * max(total_legs, 1)) * total_people
    accommodation_cost = (_hotel_pn if _hotel_pn and _hotel_pn > 0 else 300) * nights * rooms
    food_cost = 150 * travel_days * total_people

    real_tickets = [
        t for t in (attraction_tickets or [])
        if isinstance(t, dict) and t.get("name") and isinstance(t.get("price"), (int, float)) and t["price"] >= 0
    ]
    if real_tickets:
        attractions_cost = sum(float(t["price"]) for t in real_tickets) * total_people
        ticket_desc = "、".join(f"{t['name']} {t['price']:.0f}元" for t in real_tickets)
    else:
        attractions_cost = 200 * travel_days * total_people
        ticket_desc = f"按 {200}元/人/天 估算"

    misc_cost = 100 * travel_days * total_people
    total_cost = transport_cost + accommodation_cost + food_cost + attractions_cost + misc_cost
    per_person = total_cost / total_people if total_people else total_cost

    budget_max_eff = (budget_max if isinstance(budget_max, (int, float)) and budget_max > 0
                      else requirement.get("budget_max"))
    if isinstance(budget_max_eff, (int, float)) and budget_max_eff > 0:
        verdict = "人均未超预算" if per_person <= budget_max_eff else f"人均超预算（建议下调住宿档位或减天数）"
    else:
        verdict = "未提供预算上限，仅作参考"

    budget_breakdown = {
        "transport": transport_cost,
        "accommodation": accommodation_cost,
        "food": food_cost,
        "attractions": attractions_cost,
        # ★ 门票金额来源随金额一并落库：渲染器据此选说明，避免"金额按真实价、说明仍写估算"的自相矛盾
        "attractions_source": "real" if real_tickets else "estimated",
        "misc": misc_cost,
        "total": total_cost
    }

    requirement = dict(requirement)
    requirement["travel_days"] = travel_days
    if isinstance(budget_max_eff, (int, float)) and budget_max_eff > 0:
        requirement["budget_max"] = budget_max_eff

    return Command(update={
        "messages": [
            ToolMessage(
                content=f"预算汇总完成（{total_people} 人 / {nights} 晚 / {rooms} 间房）！\n"
                        f"   - 交通：{transport_cost:.2f} 元（人均 {transport_cost/total_people:.0f}）\n"
                        f"   - 住宿：{accommodation_cost:.2f} 元（{rooms} 间 × {nights} 晚）\n"
                        f"   - 餐饮：{food_cost:.2f} 元（{150}元/人/天）\n"
                        f"   - 门票：{attractions_cost:.2f} 元（{ticket_desc}）\n"
                        f"   - 其他：{misc_cost:.2f} 元\n"
                        f"总计：{total_cost:.2f} 元（{total_people} 人总价）\n"
                        f"人均：{per_person:.2f} 元\n"
                        f"预算结论：{verdict}",
                tool_call_id=runtime.tool_call_id
            )
        ],
        "budget": budget_breakdown,
        "user_requirement": requirement,
    })


# ============== 回退工具 ==============
# 全部流程步骤
ALL_STEPS = [
    "requirement_collection",       # 步骤1：需求收集
    "destination_recommendation",   # 步骤2：目的地推荐
    "transport_planning",           # 步骤3：交通规划
    "accommodation_planning",       # 步骤4：住宿规划
    "food_planning",                # 步骤5：餐饮规划
    "itinerary_generation",         # 步骤6：行程生成
    "budget_summarization",         # 步骤7：预算汇总
    "order_generation"              # 步骤8：订单生成
]

# 步骤-中文名称映射
STEP_LABELS = {
    "requirement_collection": "需求收集",
    "destination_recommendation": "目的地推荐",
    "transport_planning": "交通规划",
    "accommodation_planning": "住宿规划",
    "food_planning": "餐饮规划",
    "itinerary_generation": "行程生成",
    "budget_summarization": "预算汇总",
    "order_generation": "订单生成"
}

# 各步骤对应的状态字段，执行回退时级联清除
STEP_STATE_FIELDS = {
    "requirement_collection": ["user_requirement"],
    "destination_recommendation": ["selected_destination", "destination_options"],
    "transport_planning": [
        "selected_transport", "transport_options",
        "selected_transport_legs", "leg_cursor", "leg_selections", "leg_prices",
    ],
    "accommodation_planning": [
        "selected_accommodation_types", "accommodation_options",
        "selected_hotels", "stay_cursor", "stay_prices",
    ],
    "food_planning": ["selected_food_types", "food_options"],
    "itinerary_generation": ["itinerary", "itinerary_body"],
    "budget_summarization": ["budget"],
    "order_generation": ["order_id"]
}

# ============== 通用回退工具 ==============

@tool
def go_back_to_step(
        target_step: Literal[
            "requirement_collection",
            "destination_recommendation",
            "transport_planning",
            "accommodation_planning",
            "food_planning",
            "itinerary_generation",
            "budget_summarization"
        ],
        reason: str,
        clear_subsequent_data: bool = True,
        runtime: ToolRuntime = None
) -> Command:
    """
    回退到指定的历史步骤，允许用户重新进行规划。

    使用场景示例：
    - 用户说"我想重新选择目的地" -> target_step="destination_recommendation"
    - 用户说"我想重新制定旅行计划" -> target_step="requirement_collection"
    - 用户说"换个交通方式" -> target_step="transport_planning"
    - 用户说"住宿要求改一下" -> target_step="accommodation_planning"
    - 用户说"餐饮偏好不对" -> target_step="food_planning"
    - 用户说"行程安排需要调整" -> target_step="itinerary_generation"
    - 用户说"预算超了，重新算" -> target_step="budget_summarization"

    参数说明：
    - target_step: 要回退到的目标步骤
        * "requirement_collection" - 重新收集旅行需求（出发日期、人数、预算等）
        * "destination_recommendation" - 重新选择目的地
        * "transport_planning" - 重新选择交通方式（航班/高铁/自驾）
        * "accommodation_planning" - 重新选择住宿类型
        * "food_planning" - 重新选择餐饮偏好
        * "itinerary_generation" - 重新生成行程安排
        * "budget_summarization" - 重新计算预算

    - reason: 回退原因，用于记录和展示给用户
        示例："用户希望更换目的地为三亚"
        示例："预算超出用户预期，需要调整住宿标准"

    - clear_subsequent_data: 是否清除目标步骤之后的所有数据（默认 True）
        * True: 回退时清除后续步骤产生的选择和数据
        * False: 保留后续数据（谨慎使用，可能导致数据不一致）

    返回：
        Command 对象，包含状态更新和步骤跳转指令

    注意：
    - 不能回退到 "order_generation"（订单生成是最终步骤）
    - 回退会在消息历史中记录，方便追溯
    """

    app_logger.info(f"回退请求: target_step={target_step}, reason={reason}, clear_data={clear_subsequent_data}")

    # 验证目标步骤
    if target_step not in ALL_STEPS:
        app_logger.warning(f"无效的目标步骤: {target_step}")
        return Command(update={
            "messages": [
                ToolMessage(
                    content=f"无效的目标步骤: {target_step}",
                    tool_call_id=runtime.tool_call_id
                )
            ]
        })

    # 不允许回退到订单生成（最终步骤）
    # ★ 终态明确拒绝，不靠默认参数偷偷放行
    if target_step == "order_generation":
        app_logger.warning("尝试回退到订单生成步骤，已拒绝")
        return Command(update={
            "messages": [
                ToolMessage(
                    content="订单生成是最终步骤，无法回退到此步骤。如需修改，请回退到更早的步骤。",
                    tool_call_id=runtime.tool_call_id
                )
            ]
        })

    # 获取当前步骤（用于日志）
    current_step = runtime.state.get("current_step", "unknown") if runtime.state else "unknown"
    app_logger.info(f"执行回退: {current_step} -> {target_step}")

    # 构建状态更新
    state_update = {
        "current_step": target_step
    }

    # 如果需要清除后续数据
    cleared_fields = []
    if clear_subsequent_data:
        # ★ 级联清除：目标步及之后每一步登记的字段全部置 None
        target_index = ALL_STEPS.index(target_step)
        # ★ 产物字段挂了合并器：写 None 只会被 _keep_last_non_none 保留（等于没清），
        #   必须写 CLEAR 哨兵才真正清空，否则回退后行程/预算产物原样残留
        _clear_as = {"itinerary": CLEAR, "itinerary_body": CLEAR, "budget": CLEAR}
        for step in ALL_STEPS[target_index:]:
            for field in STEP_STATE_FIELDS.get(step, []):
                state_update[field] = _clear_as.get(field, None)
                cleared_fields.append(field)

        if cleared_fields:
            app_logger.debug(f"清除的状态字段: {cleared_fields}")

    # 获取步骤标签
    step_label = STEP_LABELS.get(target_step, target_step)

    # 构建响应消息
    response_parts = [
        f"已回退到【{step_label}】阶段",
        f"原因: {reason}"
    ]
    if clear_subsequent_data and cleared_fields:
        response_parts.append("已清除后续步骤的数据")

    state_update["messages"] = [
        ToolMessage(
            content="\n".join(response_parts),
            tool_call_id=runtime.tool_call_id
        )
    ]

    app_logger.info(f"回退完成: {target_step}, 清除字段数: {len(cleared_fields)}")

    return Command(update=state_update)

# ============== 回退守卫 ==============
def _recent_human_text(state) -> str:
    """取最近一条用户消息文本，用于判断用户是否提出回退"""
    try:
        for m in reversed((state or {}).get("messages") or []):
            if getattr(m, "type", None) == "human":
                return str(getattr(m, "content", "") or "")
    except Exception:
        pass
    return ""

from app.core.intent import detect_intent

def _wants_back(text: str, key: str) -> bool:
    """判断用户文本是否表达换/改/重新选的回退意图"""
    return detect_intent(text).wants_back(key)

def _guard_back(runtime, key: str, tool_desc: str) -> Command | None:
    """回退意图守卫：无用户明确回退意图则返回拒绝Command，返回None代表放行"""
    text = _recent_human_text(runtime.state)
    if text and _wants_back(text, key):
        return None
    app_logger.info(f"[guard] 用户未明确要求{tool_desc}，拒绝自动回退")
    return Command(update={
        "messages": [
            ToolMessage(
                content=(f"未检测到您要求『{tool_desc}』。若您确实想{'换目的地' if key=='destination' else ('换交通方式' if key=='transport' else '重新规划')}，"
                         f"请直接在对话中说明（如『换个目的地』『重新选』），我会照办；当前继续本步骤"),
                tool_call_id=runtime.tool_call_id,
            )
        ]
    })

# ============== 快捷回退工具 ==============

@tool
def go_back_to_requirement(
        reason: str = "用户需要修改旅行需求",
        runtime: ToolRuntime = None
) -> Command:
    """
    快捷回退：返回到需求收集步骤，重新开始规划。

    使用场景：
    - 用户说"我想重新规划"
    - 用户说"出发日期要改"
    - 用户说"预算变了"
    - 用户说"人数不对"
    - 用户说"从头开始"

    参数：
    - reason: 回退原因（可选，默认为"用户需要修改旅行需求"）

    效果：
    - 清除所有已收集的数据
    - 返回到最初的需求收集阶段
    """
    app_logger.info(f"快捷回退到需求收集: {reason}")

    blocked = _guard_back(runtime, "requirement", "重新规划")
    if blocked:
        return blocked

    # ★ 守卫通过后委托给通用回退：7 个快捷工具共用一个内核
    return go_back_to_step.invoke({
        "target_step": "requirement_collection",
        "reason": reason,
        "clear_subsequent_data": True,
        "runtime": runtime
    })


@tool
def go_back_to_destination(
        reason: str = "用户需要重新选择目的地",
        runtime: ToolRuntime = None
) -> Command:
    """
    快捷回退：返回到目的地推荐步骤。

    使用场景：
    - 用户说"换个目的地"
    - 用户说"这个地方不想去了"
    - 用户说"有没有其他推荐"
    - 用户说"目的地选错了"

    参数：
    - reason: 回退原因（可选）

    效果：
    - 保留用户需求
    - 清除目的地选择及后续所有数据
    """
    app_logger.info(f"快捷回退到目的地推荐: {reason}")

    blocked = _guard_back(runtime, "destination", "更换目的地")
    if blocked:
        return blocked

    return go_back_to_step.invoke({
        "target_step": "destination_recommendation",
        "reason": reason,
        "clear_subsequent_data": True,
        "runtime": runtime
    })


@tool
def go_back_to_transport(
        reason: str = "用户需要更换交通方式",
        runtime: ToolRuntime = None
) -> Command:
    """
    快捷回退：返回到交通规划步骤。

    使用场景：
    - 用户说"不想坐飞机了"
    - 用户说"改成高铁"
    - 用户说"还是自驾吧"
    - 用户说"交通方式重新选"

    参数：
    - reason: 回退原因（可选）

    效果：
    - 保留用户需求和目的地选择
    - 清除交通方式及后续数据
    """
    app_logger.info(f"快捷回退到交通规划: {reason}")

    blocked = _guard_back(runtime, "transport", "更换交通方式")
    if blocked:
        return blocked

    return go_back_to_step.invoke({
        "target_step": "transport_planning",
        "reason": reason,
        "clear_subsequent_data": True,
        "runtime": runtime
    })


@tool
def go_back_to_accommodation(
        reason: str = "用户需要调整住宿偏好",
        runtime: ToolRuntime = None
) -> Command:
    """
    快捷回退：返回到住宿规划步骤。

    使用场景：
    - 用户说"住宿要求改一下"
    - 用户说"想住民宿"
    - 用户说"酒店太贵了"
    - 用户说"换个住宿类型"

    参数：
    - reason: 回退原因（可选）

    效果：
    - 保留需求、目的地、交通方式
    - 清除住宿选择及后续数据
    """
    app_logger.info(f"快捷回退到住宿规划: {reason}")

    return go_back_to_step.invoke({
        "target_step": "accommodation_planning",
        "reason": reason,
        "clear_subsequent_data": True,
        "runtime": runtime
    })


@tool
def go_back_to_food(
        reason: str = "用户需要调整餐饮偏好",
        runtime: ToolRuntime = None
) -> Command:
    """
    快捷回退：返回到餐饮规划步骤。

    使用场景：
    - 用户说"餐饮偏好改一下"
    - 用户说"想多吃特色美食"
    - 用户说"简单点就行"

    参数：
    - reason: 回退原因（可选）

    效果：
    - 保留需求、目的地、交通、住宿
    - 清除餐饮选择及后续数据
    """
    app_logger.info(f"快捷回退到餐饮规划: {reason}")

    return go_back_to_step.invoke({
        "target_step": "food_planning",
        "reason": reason,
        "clear_subsequent_data": True,
        "runtime": runtime
    })


@tool
def go_back_to_itinerary(
        reason: str = "用户需要调整行程安排",
        runtime: ToolRuntime = None
) -> Command:
    """
    快捷回退：返回到行程生成步骤。

    使用场景：
    - 用户说"行程安排不太合理"
    - 用户说"想加点景点"
    - 用户说"太累了，减少活动"
    - 用户说"重新排一下行程"

    参数：
    - reason: 回退原因（可选）

    效果：
    - 保留所有偏好设置
    - 仅清除行程和预算数据
    """
    app_logger.info(f"快捷回退到行程生成: {reason}")

    return go_back_to_step.invoke({
        "target_step": "itinerary_generation",
        "reason": reason,
        "clear_subsequent_data": True,
        "runtime": runtime
    })


@tool
def go_back_to_budget(
        reason: str = "用户需要重新计算预算",
        runtime: ToolRuntime = None
) -> Command:
    """
    快捷回退：返回到预算汇总步骤。

    使用场景：
    - 用户说"预算超了"
    - 用户说"重新算一下费用"
    - 用户说"看看能不能便宜点"

    参数：
    - reason: 回退原因（可选）

    效果：
    - 保留行程安排
    - 仅清除预算和订单数据
    """
    app_logger.info(f"快捷回退到预算汇总: {reason}")

    return go_back_to_step.invoke({
        "target_step": "budget_summarization",
        "reason": reason,
        "clear_subsequent_data": True,
        "runtime": runtime
    })

# ============== 查询当前进度工具 ==============

@tool
def check_current_progress(
        runtime: ToolRuntime = None
) -> str:
    """
    查询当前规划进度，展示已完成和待完成的步骤。

    使用场景：
    - 用户问"现在到哪一步了"
    - 用户问"还有几步"
    - 用户问"进度如何"
    - 需要向用户汇报当前状态

    返回：
        格式化的进度信息字符串
    """
    state = runtime.state
    current_step = state.get("current_step", "requirement_collection")

    app_logger.debug(f"查询进度: current_step={current_step}")

    try:
        current_index = ALL_STEPS.index(current_step)
    except ValueError:
        app_logger.warning(f"未知的当前步骤: {current_step}")
        current_index = 0

    # 构建进度展示
    progress_lines = ["当前规划进度", ""]

    for i, step in enumerate(ALL_STEPS):
        label = STEP_LABELS.get(step, step)
        step_num = i + 1
        if i < current_index:
            progress_lines.append(f"  [{step_num}] {label} - 已完成")
        elif i == current_index:
            progress_lines.append(f"  [{step_num}] {label} - 当前步骤")
        else:
            progress_lines.append(f"  [{step_num}] {label} - 待完成")

    # 添加已收集的关键信息
    progress_lines.append("")
    progress_lines.append("已收集信息:")

    if state.get("user_requirement"):
        req = state["user_requirement"]
        progress_lines.append(f"  - 出发日期: {req.get('departure_date', '未设置')}")
        progress_lines.append(f"  - 出行天数: {req.get('travel_days', '未设置')} 天")
        progress_lines.append(f"  - 人数: {req.get('adult_count', 0)} 成人 + {req.get('children_count', 0)} 儿童")

    if state.get("selected_destination"):
        progress_lines.append(f"  - 目的地: {state['selected_destination']}")

    if state.get("selected_transport"):
        transport_labels = {"flight": "航班", "train": "高铁", "driving": "自驾"}
        progress_lines.append(
            f"  - 交通: {transport_labels.get(state['selected_transport'], state['selected_transport'])}")

    if state.get("selected_accommodation_types"):
        progress_lines.append(f"  - 住宿: {', '.join(state['selected_accommodation_types'])}")

    if state.get("selected_food_types"):
        progress_lines.append(f"  - 餐饮: {', '.join(state['selected_food_types'])}")

    app_logger.info(f"进度查询完成: 当前步骤={current_step}, 进度={current_index + 1}/{len(ALL_STEPS)}")

    # ★ 全文件唯一返回字符串的工具：只读、不改状态
    return "\n".join(progress_lines)


# ============== 导出所有回退工具 ==============

ALL_ROLLBACK_TOOLS = [
    go_back_to_step,            # 通用回退（推荐）
    go_back_to_requirement,     # 快捷：回到需求收集
    go_back_to_destination,     # 快捷：回到目的地
    go_back_to_transport,       # 快捷：回到交通
    go_back_to_accommodation,   # 快捷：回到住宿
    go_back_to_food,            # 快捷：回到餐饮
    go_back_to_itinerary,       # 快捷：回到行程
    go_back_to_budget,          # 快捷：回到预算
    check_current_progress      # 查询进度
]