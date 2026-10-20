"""手动预热城市清单到 Redis：python -m app.scripts.sync_cities

全国城市数据内置在 app.data.cities，平时接口懒加载会自动灌入；
本脚本供部署后手动预热 / 定期刷新（幂等，重复执行安全）。
"""
from app.services.city_service import sync_cities_to_redis

if __name__ == "__main__":
    ok = sync_cities_to_redis()
    print("城市清单已灌入 Redis" if ok else "灌入失败（检查 Redis 连接）")