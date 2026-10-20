"""会话服务层：/api/v1/conversations 与 /chat/* 契约路由共用，业务逻辑唯一实现"""
import uuid
from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.conversation import Conversation


async def create_conversation(db: AsyncSession, user_id: UUID, title: str = "新对话") -> Conversation:
    """创建会话（落库）"""
    conv = Conversation(user_id=user_id, title=title, status="active")
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return conv


async def list_conversations(db: AsyncSession, user_id: UUID) -> list[Conversation]:
    """用户会话列表：过滤软删除，按更新时间倒序"""
    result = await db.execute(
        select(Conversation)
        .where(Conversation.user_id == user_id)
        .where(Conversation.status != "deleted")
        .order_by(Conversation.updated_at.desc())
    )
    return list(result.scalars().all())


async def get_owned_conversation(db: AsyncSession, session_id: str, user_id: UUID) -> Conversation | None:
    """按 sessionId（前端传的字符串）查询属于该用户的会话，非法 UUID 或不存在均返回 None"""
    try:
        conv_id = uuid.UUID(session_id)
    except ValueError:
        return None
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == conv_id, Conversation.user_id == user_id  # ★ 横向越权的防线
        )
    )
    return result.scalar_one_or_none()

