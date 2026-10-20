"""线上行程质检数据源：从 ItineraryPlans 表拉取真实落库行程

替代静态样本的线上质检模式：
- 每份落库行程（status=1）反序列化回"行程正文"，title（含目的地/天数/交通/
  酒店/餐饮档次的出发信息）作为需求清单 input——title 由需求生成，最忠实；
- 三维度 GEval 全部按 positive=True 断言：线上产出的每份行程都必须达标。
"""
import json

from sqlalchemy import select, desc

from app.models.base import async_session_maker
from app.models.itinerary_plan import ItineraryPlan
from app.services.quality_check_service import plan_to_text  # 复用业务服务实现


async def _fetch(limit: int) -> list[dict]:
    """查询最近落库成功的行程，转换为评测用例（同 itinerary_cases 结构）"""
    async with async_session_maker() as db:
        result = await db.execute(
            select(ItineraryPlan)
            .where(ItineraryPlan.status == 1)  # ★ 只取落库成功的行程
            .order_by(desc(ItineraryPlan.created_at))
            .limit(limit)
        )
        rows = result.scalars().all()
        cases = []
        for row in rows:
            plan_data = row.plan_data if isinstance(row.plan_data, dict) else json.loads(row.plan_data)
            cases.append({
                # title 含"目的地+天数+出发信息"，作为需求清单输入
                "input": plan_data.get("title", ""),
                "actual_output": plan_to_text(plan_data),
                "note": f"线上质检:{str(row.plan_id)[:8]}",
                "positive": True,  # 线上产出行程必须三维度达标
            })
        return cases


def fetch_itinerary_cases(limit: int = 3) -> list[dict]:
    """同步包装（pytest 参数化在收集期同步执行）"""
    import asyncio
    return asyncio.run(_fetch(limit))