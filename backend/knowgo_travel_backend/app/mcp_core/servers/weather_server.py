"""
天气服务 MCP Server
使用高德天气 API 查询未来 4 天天气预报
"""
import os
import json
import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP

load_dotenv()

# ============== FastMCP 实例 ==============
mcp = FastMCP("weather-service")

# ============== 高德 API 配置 ==============
AMAP_API_KEY = os.getenv("AMAP_API_KEY")
AMAP_WEATHER_URL = "https://restapi.amap.com/v3/weather/weatherInfo"
AMAP_GEO_URL = "https://restapi.amap.com/v3/geocode/geo"  # 城市名 → adcode


# ============== 辅助函数 ==============

async def _resolve_adcode(client: httpx.AsyncClient, city: str) -> str | None:
    """高德地理编码：城市名 → adcode（行政区划编码）；解析失败返回 None"""
    res = await client.get(
        AMAP_GEO_URL,
        params={"key": AMAP_API_KEY, "address": city}
    )
    data = res.json()
    if data.get("status") == "1" and data.get("geocodes"):
        return data["geocodes"][0].get("adcode")
    return None


# ============== 工具定义 ==============

@mcp.tool()
async def get_weather_forecast(city: str, date: str | None = None) -> str:
    """
    查询城市未来天气预报。

    Args:
        city: 中文城市名，如 "福州"、"北京"、"杭州"；
              区分模糊地区建议用"城市+区县"（如 "杭州西湖区"）。
        date: 可选，指定日期 "YYYY-MM-DD"（仅支持未来 4 天内）；
              不传则返回全部预报（约 4 天）。

    Returns:
        JSON 字符串，包含 city / province / reporttime / casts 数组
        casts 里每一项：{ date, week, dayweather, nightweather,
                        daytemp, nighttemp, daywind, ... }
    """
    if not AMAP_API_KEY:
        return json.dumps({"error": "未配置 AMAP_API_KEY"}, ensure_ascii=False)

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            # ---------- 第 1 步：城市名 → adcode ----------
            adcode = await _resolve_adcode(client, city)
            if not adcode:
                return json.dumps(
                    {"error": f"无法解析城市: {city}，请用更规范的名称（如 '福州市'）"},
                    ensure_ascii=False,
                )

            # ---------- 第 2 步：拿 adcode 查天气 ----------
            response = await client.get(
                AMAP_WEATHER_URL,
                params={
                    "key": AMAP_API_KEY,
                    "city": adcode,
                    "extensions": "all",   # "all" = 预报天气；"base" = 实况
                    "output": "JSON",
                }
            )
            data = response.json()

            if data.get("status") != "1":
                return json.dumps({
                    "error": data.get("info", "查询失败"),
                    "infocode": data.get("infocode"),
                }, ensure_ascii=False)

            forecasts = data.get("forecasts", [])
            if not forecasts:
                return json.dumps({"error": "未找到天气数据"}, ensure_ascii=False)

            forecast = forecasts[0]
            casts = forecast.get("casts", [])

            # ---------- 第 3 步：如果指定日期，就筛出那一天 ----------
            if date:
                matched = [c for c in casts if c.get("date") == date]
                casts = matched if matched else casts

            result = {
                "city": forecast.get("city"),
                "adcode": forecast.get("adcode"),
                "province": forecast.get("province"),
                "reporttime": forecast.get("reporttime"),
                "casts": casts,
            }
            return json.dumps(result, ensure_ascii=False, indent=2)

        except httpx.TimeoutException:
            return json.dumps({"error": "请求超时"}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)


if __name__ == "__main__":
    # stdio 传输：Server 从 stdin 读请求、往 stdout 写响应
    # 由 MCP Client 起一个子进程运行本文件
    mcp.run(transport="stdio")