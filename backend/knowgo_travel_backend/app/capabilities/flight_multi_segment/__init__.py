"""多段航班行程 Agent Skill 包（skill_name = flight_multi_segment）

对外只需记两个工具：
  - query_flight_multi_segment_tool ：并行查全部段航班候选 → 出参 payload 下发前端
  - submit_flight_multi_segment_tool：接收前端回传的选中航班 → 仅基础合法性校验（不做时间校验）

契约定义见 flight_skill_schema.py，实现见 flight_agent_tool.py，状态机接线见 flight_flow.py。
"""
from app.capabilities.flight_multi_segment.flight_agent_tool import (
    build_flight_multi_segment_payload,
    query_flight_multi_segment_tool,
    submit_flight_multi_segment_tool,
    validate_flight_submission,
)
from app.capabilities.flight_multi_segment.flight_skill_schema import (
    FLIGHT_MULTI_SEGMENT_SKILL,
    FlightMultiSegmentPayload,
    FlightSubmitResult,
)

__all__ = [
    "FLIGHT_MULTI_SEGMENT_SKILL",
    "FlightMultiSegmentPayload",
    "FlightSubmitResult",
    "build_flight_multi_segment_payload",
    "validate_flight_submission",
    "query_flight_multi_segment_tool",
    "submit_flight_multi_segment_tool",
]