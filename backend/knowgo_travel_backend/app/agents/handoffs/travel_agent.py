"""
Handoffs 主 Agent
一个 Agent + 中间件实现整个旅行规划流程
"""
from app.tools.mcp_tools import get_all_mcp_tools, apply_train_arg_guard
from app.tools.router_query import query_destination_info
# 并行改造：query_transport_options（mock 链路）已废弃移除，交通步骤改用真实 MCP 工具
from langchain.agents import create_agent
from app.config import settings
from app.core.state import TravelState
from app.core.checkpointer import get_checkpointer
from app.core.store import get_store
from langchain_openai import ChatOpenAI
from app.core.middleware import create_step_config_middleware
from app.tools.memory_tools import (
    update_travel_style_tool,
    update_dietary_restriction_tool,
    update_food_preference_tool,
    add_travel_record_tool,
    update_accommodation_preference_tool  # 修复存量 bug：住宿步骤中间件引用了此工具但主表漏注册
)
from app.tools.state_transition import (
    record_requirement_tool,
    select_destination_tool,
    select_transport_tool,
    select_accommodation_tool,
    select_food_tool,
    select_accommodation_food_tool,  # 一站式住宿+餐饮确认（总工具表注册，防 unknown tool names）
    generate_itinerary_tool,
    submit_itinerary_body_tool,  # 结构化行程提交（正文由代码渲染）
    summarize_budget_tool,
    # generate_order_tool,  # 订单环节已按需求移除：无需订单，行程清单即最终产物
    ALL_ROLLBACK_TOOLS
)
from app.utils.logger import app_logger

# ============== 初始化 LLM ==============

def get_llm():
    """获取配置好的千问模型"""
    return ChatOpenAI(
        model=settings.qwen_model_name,
        base_url=settings.qwen_base_url,
        api_key=settings.dashscope_api_key,
        temperature=settings.qwen_temperature,
        max_tokens=settings.qwen_max_tokens,
        streaming=True
    )


# ============== 创建 Agent ==============

async def create_travel_agent():
    """
    创建 Handoffs 旅行规划 Agent

    返回：
        编译好的 Agent（可直接调用）
    """

    app_logger.info("创建 Travel Agent（带审批）...")

    llm = get_llm()
    all_mcp_tools = await get_all_mcp_tools()
    # 注册侧同源套用车票守卫。ToolNode 是按名到本注册表取对象执行的，
    # 若此处不套，middleware 注入侧的 _ArgGuardTool 会被绕过（参数兜底/结果裁剪全失效）
    all_mcp_tools = apply_train_arg_guard(all_mcp_tools)

    # 异步创建中间件（预加载配置）
    step_config_middleware = await create_step_config_middleware()

    all_tools = [
        record_requirement_tool,
        select_destination_tool,
        select_transport_tool,
        select_accommodation_tool,
        select_food_tool,
        select_accommodation_food_tool,  # 一站式住宿+餐饮确认
        generate_itinerary_tool,
        submit_itinerary_body_tool,
        summarize_budget_tool,
        # generate_order_tool,  # 订单环节已移除，总工具表不再注册
        *ALL_ROLLBACK_TOOLS,
        query_destination_info,
        # 并行改造：query_transport_options 已从总表移除（mock 假数据链路废弃）
        *all_mcp_tools,
        update_travel_style_tool,
        update_dietary_restriction_tool,
        update_food_preference_tool,
        add_travel_record_tool,
        update_accommodation_preference_tool  # 修复存量 bug：补注册，住宿步骤中间件按名引用此工具
    ]

    # 使用 PostgreSQL 持久化 Checkpointer（替代 MemorySaver）
    checkpointer = await get_checkpointer()
    store = await get_store()
    agent = create_agent(
        model=llm,
        tools=all_tools,
        state_schema=TravelState,
        middleware=[step_config_middleware],
        checkpointer=checkpointer,
        store=store
    )

    app_logger.info("✅ Travel Agent（带审批）创建完成")

    return agent