"""
数据库模型
"""
from app.models.base import Base
from app.models.user import User
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.itinerary_plan import ItineraryPlan  # ★ 本章新增的第四张表：不登记就建不出来

__all__ = ["Base", "User", "Conversation", "Message", "ItineraryPlan"]