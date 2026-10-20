"""
消息 API（资源式）：发消息 + 拉历史

路径按资源式扁平约定挂在 /api/v1/messages（不嵌套 conversations/{id}/messages）：
- POST /api/v1/messages  body {conversation_id, content} → SSE 流
- GET  /api/v1/messages?conversation_id=xxx → {conversation, messages}

响应体统一由 wrap_json_middleware 包成 {code,msg,data}；SSE 流是 text/event-stream，不受包裹影响。
"""
import json
from fastapi import APIRouter, Depends, HTTPException, status, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.base import get_db
from app.models.user import User
from app.models.message import Message
from app.schemas.message import MessageSendRequest
from app.api.dependencies import get_current_user
from app.services.chat_contract_service import run_chat_turn
from app.services.conversation_service import get_owned_conversation

router = APIRouter(prefix="/messages", tags=["消息"])

# SSE 响应头：禁用各级缓冲，保证逐字流式
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",  # ★ 禁用 Nginx 缓冲（本地没事、一上 Nginx 就"卡顿式流式"的那个坑）
}


def _sse(event: dict) -> str:
    """统一事件 → SSE data 帧（剔除 None 字段）"""
    # ★ 剔掉值为 None 的字段：大部分帧 code 是 None，没必要给前端送 "code": null
    frame = {k: v for k, v in event.items() if v is not None}
    # ★ SSE 帧 = "data: " + JSON + 两个换行；少一个换行前端就一直等
    return f"data: {json.dumps(frame, ensure_ascii=False)}\n\n"


@router.post("")
async def send_message(
        req: MessageSendRequest,
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
):
    """发消息：JWT 走标准 Authorization 头（fetch 实现，非原生 EventSource），返回 SSE 流"""
    conv = await get_owned_conversation(db, req.conversation_id, user.id)
    if not conv:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")

    async def event_generator():
        # ★ run_chat_turn 立即返回 EventStream（编排在后台跑），这里马上就能开始读
        stream = await run_chat_turn(db, str(user.id), req.conversation_id, req.content)
        async for event in stream:
            # ★ 每 yield 一帧，StreamingResponse 就立刻写进响应体推给浏览器
            yield _sse(event)

    return StreamingResponse(
        event_generator(), media_type="text/event-stream", headers=_SSE_HEADERS
    )

@router.get("")
async def list_messages(
        conversation_id: str = Query(..., min_length=1, description="会话 id"),
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
):
    """历史消息：{conversation, messages}（assistant 全文/清单回放存于 message.extra_info）"""
    conv = await get_owned_conversation(db, conversation_id, user.id)
    if not conv:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")

    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conv.id)
        .order_by(Message.created_at)
    )
    messages = result.scalars().all()
    return {"conversation": conv.to_dict(), "messages": [m.to_dict() for m in messages]}