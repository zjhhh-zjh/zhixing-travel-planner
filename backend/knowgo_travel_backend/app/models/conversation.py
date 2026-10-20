"""
会话模型
"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, JSON, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
from app.models.base import Base


class Conversation(Base):
    """会话表"""

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    ) #主键

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),  # ★ 外键指向 users（复数）
        index=True
    ) #外键

    title: Mapped[str] = mapped_column(String(200), default="新对话")

    # 状态：active, archived, deleted
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)  # ★ 软删只改这一列

    # 元数据（JSON 格式）
    extra_info: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())

    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), onupdate=func.now())

    # 关系
    user: Mapped["User"] = relationship(back_populates="conversations")
    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at"  # ★ 字符串懒引用：访问 conv.messages 自动按时间排
    )