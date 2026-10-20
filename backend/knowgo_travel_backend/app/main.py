"""
FastAPI 应用入口
"""
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from app.config import settings
from app.utils.logger import app_logger
from app.api.v1 import conversations, cities, itinerary_plans, messages, users, skills  # ★ 本章新增 messages / itinerary_plans / skills
from app.core.response import (
    global_http_middleware,
    validation_exception_handler,
    wrap_json_middleware,  # 统一 {code,msg,data} 包裹
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    import asyncio

    # 打印当前事件循环信息（调试用）
    loop = asyncio.get_running_loop()
    app_logger.info(f"FastAPI 使用的事件循环: {type(loop).__name__}")

    from app.core.checkpointer import checkpointer_lifespan
    from app.mcp_core.client import MCPClientManager
    from app.core.store import store_lifespan
    from app.models.base import init_db

    app_logger.info("🚀 启动应用...")

    # 建表：业务表（users/conversations/messages/itinerary_plans 等）首次启动自动创建
    await init_db()
    app_logger.info("✅ 业务表已就绪")

    async with checkpointer_lifespan():
        app_logger.info("✅ Checkpointer 已就绪")

        # 2. 启动 Store (在这里加入)
        async with store_lifespan():
            app_logger.info("✅ Store 已就绪")

            # 3. 初始化 MCP
            mcp = await MCPClientManager.get_instance()
            app_logger.info("✅ MCP 服务初始化成功")

            yield

            # --- 关闭流程 (自动按相反顺序执行 context manager 的 exit，这里手动关闭 MCP) ---

            await mcp.close()
            app_logger.info("MCP 服务已关闭")

        # store_lifespan 的 exit 会在这里自动执行

    # checkpointer_lifespan 的 exit 会在这里自动执行

    app_logger.info("应用已关闭")


app = FastAPI(
    title="LangGraph 旅行规划系统",
    description="企业级多 Agent 旅行规划服务",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 全局异常兜底中间件 + 参数校验器（{code,msg,data} 契约）
app.middleware("http")(global_http_middleware)
# 响应包裹中间件：放在最后注册 → 最外层，保证异常兜底产出的包裹体不再二次包裹
app.middleware("http")(wrap_json_middleware)  # ★ 最后注册 = 最外层
app.add_exception_handler(RequestValidationError, validation_exception_handler)

# 全部接口统一收编到 /api/v1（资源式扁平命名）
app.include_router(users.router, prefix="/api/v1")
app.include_router(cities.router, prefix="/api/v1")  # 全国城市（出发地单选/目的地多选）
app.include_router(conversations.router, prefix="/api/v1")
app.include_router(messages.router, prefix="/api/v1")  # 发消息（SSE）+ 历史消息
app.include_router(itinerary_plans.router, prefix="/api/v1")  # 行程清单
app.include_router(skills.router, prefix="/api/v1")  # Agent UI Skill 回提交口（聊天内嵌卡片）

image_folder = os.path.join(os.getcwd(), "image")
os.makedirs(image_folder, exist_ok=True)
app.mount("/image", StaticFiles(directory=image_folder))


@app.get("/")
async def root():
    return {
        "status": "healthy",
        "service": "LangGraph Travel Planner",
        "version": "1.0.0",
        "docs": "/docs"
    }