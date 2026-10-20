"""
行程清单 API（资源式）

- GET /api/v1/itinerary-plans/{plan_id} → data 直接为 TravelItinerary JSON

归属校验下沉到服务层 get_owned_plan（join 会话表），路径不再需要 sessionId；
响应体由 wrap_json_middleware 统一包成 {code,msg,data}。
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import get_db
from app.models.user import User
from app.api.dependencies import get_current_user
from app.services.itinerary_plan_service import get_owned_plan

router = APIRouter(prefix="/itinerary-plans", tags=["行程清单"])


@router.get("/{plan_id}")
async def get_itinerary_plan(
        plan_id: str,
        user: User = Depends(get_current_user),  # type: ignore
        db: AsyncSession = Depends(get_db),
):
    """行程清单：data 直接为 TravelItinerary JSON（前端按 res.data 渲染清单页）"""
    # ★ 归属校验下沉在服务层（join 会话表）：别人猜到 plan_id 也读不走
    plan = await get_owned_plan(db, plan_id, user.id)
    if not plan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="行程清单不存在")
    return plan.plan_data