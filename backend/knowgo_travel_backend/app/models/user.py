"""
用户模型
"""
import uuid
from typing import Optional
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, JSON, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
from app.models.base import Base


class User(Base):
    """用户表"""
    __tablename__ = "users"  # ★ user 是 PG 保留字，改用复数
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4  # ★ 主键在 Python 端生成，写库前就有 id
    )

    # ========== 通用账密登录（H5/管理后台用）转 nullable ==========
    username: Mapped[Optional[str]] = mapped_column(String(50), unique=True, index=True, nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(100), unique=True, index=True, nullable=True)
    password_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # 用户偏好（JSON 格式）
    preferences: Mapped[dict] = mapped_column(JSON, default=dict,nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=func.now(),
        onupdate=func.now()
    )

    # 关系
    conversations: Mapped[list["Conversation"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan"
    )
    #back_populates:双向绑定
    #back_populates="user" 的作用是告诉 SQLAlchemy：“在另一个模型里，有一个叫 user 的属性，它和我是互为反向引用的。”
    '''
    cascade="all, delete-orphan" 是最常用、最严格的级联设置，它包含两个部分：
    all:代表开启所有标准的级联操作,你创建了一个新用户，同时给他创建了一个新对话
    delete-orphan:删除父对象，他对应的Conversations会被自动删除'''