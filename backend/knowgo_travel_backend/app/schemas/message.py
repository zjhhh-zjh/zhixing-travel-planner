"""
消息相关的 Pydantic 模型
"""
from pydantic import BaseModel, Field


class MessageSendRequest(BaseModel):
    """发消息入参：资源式扁平契约，会话 id 走 body（与路径 /api/v1/messages 对齐）"""
    conversation_id: str = Field(..., min_length=1)  # ★ 只查非空，UUID 格式交给领域层
    content: str = Field(..., min_length=1)