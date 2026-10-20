"""
行程清单模型
存储结构化行程 JSON，供前端"行程清单已生成，点击查看"拉取渲染
"""
import uuid
from datetime import datetime
from sqlalchemy import String, DateTime, JSON, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID
from app.models.base import Base


class ItineraryPlan(Base):
    """行程清单表"""

    __tablename__ = "itinerary_plans"

    # 业务层生成的 plan_id（UUID 字符串），前端在 itinerary_plans 帧中拿到后回传查询
    # ★ plan_id 由业务层 str(uuid4()) 显式生成：落库后要立刻 return 给前端
    plan_id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)

    # 所属会话（对应前端契约的 sessionId，即 LangGraph 的 thread_id）
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversation.id", ondelete="CASCADE"),
        index=True,
    )

    # 完整结构化行程数据（前端 TravelItinerary：title + itinerary_list，含经纬度/图片）
    plan_data: Mapped[dict] = mapped_column(JSON)

    # 生成状态：1 成功，2 失败
    status: Mapped[int] = mapped_column(default=1)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())