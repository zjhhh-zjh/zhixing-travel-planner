"""全国城市接口

GET /api/v1/cities?q=关键词 —— 前端"出发地单选 / 目的地多选"城市选择器数据源。
返回 data: [{name, province}, ...]，q 为空返回全量（前端本地缓存后模糊过滤）。
"""
from fastapi import APIRouter, Query

from app.services.city_service import list_cities

router = APIRouter(prefix="/cities", tags=["城市"])


@router.get("")
async def get_cities(q: str = Query("", description="模糊匹配城市名/省份，空则返回全量")):
    """获取全国城市列表（Redis 优先，自动预热）"""
    return list_cities(q)