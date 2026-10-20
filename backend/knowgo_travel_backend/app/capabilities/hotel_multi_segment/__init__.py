"""
多段住宿选择 Agent Skill 包（skill_name = hotel_multi_segment）

对外只需记两个工具：
  - query_hotel_multi_segment_tool ：按段**并行**调用已有酒店 MCP 工具 → 逐段解析 →
                                      组装 payload 下发前端（每段独立判定 hotel / brand 模式）
  - submit_hotel_multi_segment_tool：接收前端一次性回传的全部住宿段选择 → 仅基础合法性校验

契约定义见 hotel_skill_schema.py，实现见 hotel_agent_tool.py；
解析核心（三级兜底：JSON → 结构化取值 → 文本行解析）见同包 hotel_parser.py。
"""
from app.capabilities.hotel_multi_segment.hotel_agent_tool import (
    build_hotel_multi_segment_payload,
    hotel_rank,
    pick_best_hotel,
    query_hotel_multi_segment_tool,
    submit_hotel_multi_segment_tool,
    validate_hotel_multi_submission,
)
from app.capabilities.hotel_multi_segment.hotel_skill_schema import (
    HOTEL_MULTI_SEGMENT_SKILL,
    HotelMultiSegmentItem,
    HotelMultiSegmentPayload,
    HotelMultiSegmentSubmitPayload,
    HotelMultiSegmentSubmitResult,
)

__all__ = [
    "HOTEL_MULTI_SEGMENT_SKILL",
    "HotelMultiSegmentItem",
    "HotelMultiSegmentPayload",
    "HotelMultiSegmentSubmitPayload",
    "HotelMultiSegmentSubmitResult",
    "build_hotel_multi_segment_payload",
    "hotel_rank",
    "pick_best_hotel",
    "validate_hotel_multi_submission",
    "query_hotel_multi_segment_tool",
    "submit_hotel_multi_segment_tool",
]