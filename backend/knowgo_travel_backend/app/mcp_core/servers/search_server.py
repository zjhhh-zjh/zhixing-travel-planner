"""
搜索服务 MCP Server
使用 Tavily API 搜索旅游相关信息
"""
import os
import json
from dotenv import load_dotenv
import httpx
from fastmcp import FastMCP

load_dotenv()

mcp = FastMCP("search-service")

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
TAVILY_URL = "https://api.tavily.com/search"


@mcp.tool()
async def search_travel_info(query: str, max_results: int = 5) -> str:
    """
    搜索旅游相关信息（景点、攻略、美食、住宿等）。

    Args:
        query: 搜索关键词，如 "西安必去景点"、"成都美食推荐"、
               "北京交通指南"、"杭州住宿攻略" 等
        max_results: 返回结果数量，默认 5 条，最多 10 条

    Returns:
        JSON 字符串，包含：
        - answer: Tavily 生成的问题摘要（相当于"直接的回答"）
        - results: 详细结果列表（title / url / content）
    """
    if not TAVILY_API_KEY:
        return json.dumps({"error": "未配置 TAVILY_API_KEY"}, ensure_ascii=False)

    max_results = min(max_results, 10)

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(
                TAVILY_URL,
                json={
                    "api_key": TAVILY_API_KEY,
                    "query": query,
                    "search_depth": "basic",       # basic / advanced 两种深度
                    "max_results": max_results,
                    "include_answer": True,        # 让 Tavily 直接给摘要
                },
            )

            if response.status_code != 200:
                return json.dumps(
                    {"error": f"API 请求失败: {response.status_code}"},
                    ensure_ascii=False,
                )

            data = response.json()
            result = {
                "query": query,
                "answer": data.get("answer"),
                "results": [
                    {
                        "title": r.get("title"),
                        "url": r.get("url"),
                        "content": r.get("content", "")[:300],  # 截断，防泄漏
                    }
                    for r in data.get("results", [])
                ],
            }
            return json.dumps(result, ensure_ascii=False, indent=2)

        except httpx.TimeoutException:
            return json.dumps({"error": "请求超时"}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="stdio")