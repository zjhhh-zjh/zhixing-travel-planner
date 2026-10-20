"""
多段高铁行程 Agent Skill 包（skill_name = train_multi_segment）

对外只需记两个工具：
  - query_train_multi_segment_tool ：并行查全部段 12306 真实班次 → 出参 payload 下发前端
  - submit_train_multi_segment_tool：接收前端回传的选中车次 → 仅基础合法性校验（不做时间校验）

契约定义见 train_skill_schema.py，实现见 train_agent_tool.py。
"""
from app.capabilities.train_multi_segment.train_agent_tool import (
    build_train_multi_segment_payload,
    query_train_multi_segment_tool,
    submit_train_multi_segment_tool,
    train_score,
    validate_train_submission,
)
from app.capabilities.train_multi_segment.train_skill_schema import (
    TRAIN_MULTI_SEGMENT_SKILL,
    TrainMultiSegmentPayload,
    TrainSubmitResult,
)

__all__ = [
    "TRAIN_MULTI_SEGMENT_SKILL",
    "TrainMultiSegmentPayload",
    "TrainSubmitResult",
    "build_train_multi_segment_payload",
    "train_score",
    "validate_train_submission",
    "query_train_multi_segment_tool",
    "submit_train_multi_segment_tool",
]