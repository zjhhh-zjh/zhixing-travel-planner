"""全国城市数据服务

数据源：app.data.cities 内置清单（342 个地级市/直辖市）。
- list_cities：接口读取入口，Redis 命中直读（key: cities:cn，TTL 7 天），
  miss 时自动灌入再读；Redis 不可用回落内置清单，功能不中断。
- sync_cities_to_redis：幂等灌入脚本入口（python -m app.scripts.sync_cities），
  也供接口懒加载复用。
"""
import json

import redis as redis_lib

from app.config import settings
from app.utils.logger import app_logger
from app.data.cities import CITY_PROVINCE

_CITY_KEY = "cities:cn"
_CITY_TTL = 7 * 24 * 3600  # 城市清单变化极低频，7 天足够


def _redis() -> redis_lib.Redis:
    """同步 Redis 客户端（对齐 app.rag.cache 先例；城市数据量小，阻塞可忽略）"""
    return redis_lib.Redis.from_url(settings.redis_url, decode_responses=True)


def sync_cities_to_redis() -> bool:
    """把内置城市清单整表重写进 Redis（幂等，重复执行只覆盖）"""
    try:
        payload = [{"name": n, "province": p} for n, p in CITY_PROVINCE.items()]
        _redis().set(_CITY_KEY, json.dumps(payload, ensure_ascii=False), ex=_CITY_TTL)
        app_logger.info(f"[cities] 城市清单已灌入 Redis: {len(payload)} 个")
        return True
    except Exception as e:
        app_logger.warning(f"[cities] Redis 灌入失败: {e}")
        return False


def list_cities(q: str = "") -> list[dict]:
    """读取城市列表（Redis 优先，miss 自动灌入）；q 非空时按城市名/省份模糊匹配"""
    cities: list[dict] | None = None
    try:
        raw = _redis().get(_CITY_KEY)
        if raw:
            cities = json.loads(raw)
    except Exception as e:
        app_logger.warning(f"[cities] Redis 读取失败，回落内置清单: {e}")
    if cities is None:
        cities = [{"name": n, "province": p} for n, p in CITY_PROVINCE.items()]  # ★ Redis 读不到：回落内置清单
        sync_cities_to_redis()  # ★ 顺手回灌，下次直接命中
    q = q.strip()
    if not q:
        return cities
    return [c for c in cities if q in c["name"] or q in c["province"]]