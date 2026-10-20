"""多段自驾行程 Agent Skill 包（skill_name = driving_multi_segment）

对外只需记两个工具：
  - query_driving_multi_segment_tool ：并行查全部段高德驾车路线 → 出参 payload 下发前端
  - submit_driving_multi_segment_tool：接收前端回传的段确认 → 仅基础合法性校验（只读确认）

契约定义见 driving_skill_schema.py，实现见 driving_agent_tool.py，状态机接线见 driving_flow.py。
"""
from app.capabilities.driving_multi_segment.driving_agent_tool import (
    build_driving_multi_segment_payload,
    query_driving_multi_segment_tool,
    submit_driving_multi_segment_tool,
    validate_driving_submission,
)
from app.capabilities.driving_multi_segment.driving_skill_schema import (
    DRIVING_MULTI_SEGMENT_SKILL,
    DrivingMultiSegmentPayload,
    DrivingSubmitResult,
)

__all__ = [
    "DRIVING_MULTI_SEGMENT_SKILL",
    "DrivingMultiSegmentPayload",
    "DrivingSubmitResult",
    "build_driving_multi_segment_payload",
    "validate_driving_submission",
    "query_driving_multi_segment_tool",
    "submit_driving_multi_segment_tool",
]