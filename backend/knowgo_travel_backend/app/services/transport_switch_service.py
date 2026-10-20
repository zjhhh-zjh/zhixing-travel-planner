import re
from typing import Any, Optional

from app.utils.logger import app_logger

_TRANSPORT_STEP = "transport_planning"

# 方式 → 用户可读名（改选回执、降级说明共用）
MODE_LABELS = {"train": "高铁", "flight": "航班", "driving": "自驾"}

# 口语词 → 方式。与三个 flow 的表单词表同口径，另补口语变体；命名分组识别方式
_MODE_RE = re.compile(
    r"(?P<train>高铁|动车|火车|铁路)"
    r"|(?P<flight>飞机|航班|机票|乘机|飞过去)"
    r"|(?P<driving>自驾|开车|驾车)"
)

# 前置否定：否定词后最多隔2个字就是方式词
_NEG_BEFORE = re.compile(
    r"(不坐|不乘|不搭|不要|不想|不用|不选|不订|不买|不开|不走|不考虑|"
    r"别坐|别乘|别订|别选|别开|放弃|取消|拒绝)[^，,。；;！!？?\s]{0,2}$"
)
# 后置否定：方式词后最多隔2个字出现否定
_NEG_AFTER = re.compile(
    r"^[^，,。；;！!？?\s]{0,2}(没有|没票|没了|不行|不要|不坐|算了|查不到|订不到|买不到|"
    r"不可用|不方便|不考虑|去不了|到不了|太贵|太慢|太累|太远|取消)"
)

# 卡片提交后的合成续跑发言以此开头，不属于用户手动改选
_SYNTH_PREFIXES = ("我已按卡片", "我已确认全部")


def current_transport_mode(state: dict) -> Optional[str]:
    """当前生效的交通方式，口径与卡片路由完全一致（高铁 > 航班 > 自驾）
    复用三个flow的 *_mode_requested 判定，保证改选前后路由判定一致。
    """
    from app.capabilities.driving_multi_segment.driving_flow import driving_mode_requested
    from app.capabilities.flight_multi_segment.flight_flow import flight_mode_requested
    from app.capabilities.train_multi_segment.train_flow import train_mode_requested
    if train_mode_requested(state):
        return "train"
    if flight_mode_requested(state):
        return "flight"
    if driving_mode_requested(state):
        return "driving"
    return None


def detect_transport_switch(text: str, current: Optional[str] = None) -> Optional[str]:
    """从用户原话识别改选目标（纯函数，无IO）
    规则：
    - 方式词前后紧邻否定词视为不选择该方式；
    - 在肯定提及中取最后一个与当前方式不同的作为目标；
    - 仅重申当前方式、无方式提及则返回 None，不修改状态。
    """
    text = str(text or "").strip()
    if not text or text.startswith(_SYNTH_PREFIXES):
        return None
    positives: list[str] = []
    for m in _MODE_RE.finditer(text):
        before = text[max(0, m.start() - 6):m.start()]
        after = text[m.end():m.end() + 8]
        if _NEG_BEFORE.search(before) or _NEG_AFTER.search(after):
            continue
        positives.append(m.lastgroup)
    for mode in reversed(positives):
        if mode != current:
            return mode
    return None


async def switch_transport_mode(agent: Any, config: dict, state: dict, target: str) -> dict:
    """把交通方式改写为target（单选）并落库；返回写入后的状态视图
    - transport_types 设置为单选列表，避免优先级覆盖；
    - 清空交通步旧产物字段，防止残留旧方式干扰路由；
    - 仅执行一次状态更新，避免多次更新冲突。
    """
    from app.tools.state_transition import STEP_STATE_FIELDS
    req = dict(state.get("user_requirement") or {})
    req["transport_types"] = [target]
    update: dict = {field: None for field in STEP_STATE_FIELDS.get(_TRANSPORT_STEP, [])}
    update["user_requirement"] = req
    await agent.aupdate_state(config, update)
    return {**state, **update}


async def apply_transport_switch(agent: Any, config: dict, state: dict, content: str) -> Optional[str]:
    """交通步前处理：识别用户改选意图，先落库再执行Agent
    返回新方式中文名用于回执；非交通步、无改选意图、落库失败返回None。
    落库失败仅记录日志，不中断本轮对话。
    """
    if (state or {}).get("current_step") != _TRANSPORT_STEP:
        return None
    current = current_transport_mode(state)
    target = detect_transport_switch(content, current)
    if not target:
        return None
    try:
        await switch_transport_mode(agent, config, state, target)
    except Exception as exc:
        app_logger.warning(f"⚠️ 交通方式改选落库失败（本轮按原方式继续）：{exc}")
        return None
    app_logger.info(
        f"🔀 交通步对话内改选：{current or '未指定'} → {target}（用户原话：{str(content)[:40]}）"
    )
    return MODE_LABELS[target]


__all__ = [
    "MODE_LABELS",
    "current_transport_mode",
    "detect_transport_switch",
    "switch_transport_mode",
    "apply_transport_switch",
]
