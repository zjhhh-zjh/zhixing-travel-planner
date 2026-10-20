"""
统一事件流契约

Agent 核心只产出 AgentEvent，对传输协议（SSE）无感知；
适配器负责把 AgentEvent 序列化为协议帧：
- SSE 帧：data: {"role", "content", "code"?}

role 取值 = 前端契约，任何一侧不得私自变更。
"""
from typing import Any, TypedDict


class AgentEvent(TypedDict):
    """统一事件结构"""
    role: str
    content: Any
    code: int | None  # 仅 done/end 帧携带状态码，其余为 None


# 事件类型常量（= 前端解析的 role 字段）
ROLE_ASSISTANT = "assistant"        # 流式文本 chunk
ROLE_ASSISTANT_RESET = "assistant_reset"  # 撤销本回合已推送文本（择优回退时使用）
ROLE_SKILLS = "skills"              # 技能执行状态 {"skill_name","title","desc"}
# 结构化 UI Skill 帧：{"skill_name","data"} —— 前端按 skill_name 查 UI_SKILL_REGISTRY
# 把组件内嵌渲染在聊天流里（与上面的状态提示帧区分：这条携带完整 payload，是要交互的）
ROLE_SKILL_UI = "skill_ui"  # ★ 本章主角：携带完整 payload、要交互的卡片帧
ROLE_ITINERARY = "itinerary_plans"  # 行程清单状态 {"status","id","message"}
ROLE_DONE = "done"                  # 本轮结束
ROLE_ERROR = "error"                # 业务错误
ROLE_END = "end"                    # 连接级终止（如鉴权失败 code=401）


def make_event(role: str, content: Any = None, code: int | None = None) -> AgentEvent:
    """构造统一事件"""
    return {"role": role, "content": content, "code": code}