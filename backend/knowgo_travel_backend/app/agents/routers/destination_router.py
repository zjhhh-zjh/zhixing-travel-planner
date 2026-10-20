import json
from typing import TypedDict, Annotated, Literal
from operator import add                      # reducer 用的"列表拼接"函数
from pydantic import BaseModel, Field
from langchain_community.chat_models import ChatTongyi
from langchain.agents import create_agent
from langgraph.graph import StateGraph, START, END
from langgraph.types import Send              # 动态扇出的"信封"
from app.config import settings

from app.utils.logger import app_logger


class Classification(TypedDict):
    # 分类器产出的"一个待派任务"
    # agent 用 Literal 钉死取值：它下一步要当"节点名"用，拼错会跳到不存在的节点报错
    agent: Literal["explore", "weather"]
    query: str                                 # 派给该 agent 的子查询


class AgentOutput(TypedDict):
    # 一个分支跑完交回来的"一条结果"
    agent_name: str                            # 出处章：并行产出顺序不定，盖章才分得清谁是谁
    result: str


class DestinationRouterState(TypedDict):
    original_query: str                        # 用户原始问题
    destination: str                           # 目的地城市
    classifications: list[Classification]       # 分类器决定要派的任务列表
    # ★ 默认更新规则是"覆盖"——后写的盖掉先写的，那样两个分支跑完只剩一个结果。
    #   加 Annotated[..., add] 声明 reducer 后，框架改用 add（列表拼接）合并：
    #   explore 交 [A]、weather 交 [B] → [A, B]，一条不丢、还不用你加锁。
    agent_results: Annotated[list[AgentOutput], add]
    final_report: str

class ClassificationResult(BaseModel):
    # 给 with_structured_output 用的"输出模具"：强制 LLM 按这个结构吐
    classifications: list[Classification] = Field(description="要调用的 Agent 列表及其子查询")


def classifier_node(state: DestinationRouterState) -> dict:
    """分类器节点：看用户问题 → 决定派哪些 Agent"""
    app_logger.info(f"分类器分析查询: {state['original_query']}")

    llm = ChatTongyi(model=settings.qwen_model_name, api_key=settings.dashscope_api_key)
    structured_llm = llm.with_structured_output(ClassificationResult)   # 输出被约束成上面的模具

    result = structured_llm.invoke([
        {"role": "system", "content": """你是旅行查询分类专家。分析用户查询，决定需要调用哪些 Agent：

**可用 Agent**：
- explore：景点攻略、美食推荐、住宿信息、交通指南（从知识库检索）
- weather：实时天气信息（调用天气 API）

**分类规则**：
1. 涉及景点、美食、住宿、攻略 → explore
2. 涉及天气、气温、降雨 → weather
3. 综合性查询（如"推荐XX旅游"）→ 两个都调用"""},
        {"role": "user", "content": f"目的地：{state['destination']}\n查询：{state['original_query']}"},
    ])

    # ★ 确定性补齐：目的地步要求必展示实时天气，但 LLM 偶发只返回 explore、漏掉 weather
    #   （实测某次天津查询只回 1 个 Agent → 天气整块没跑 → 前端天气栏空了）。
    #   dict(c) 把 Pydantic 对象转普通 dict，方便下面 .get 判断和 append 补一条。
    items = [dict(c) for c in result.classifications]
    if not any(c.get("agent") == "weather" for c in items):
        items.append({"agent": "weather", "query": f"{state['destination']} 天气"})
        app_logger.info("✅ LLM 未选 weather，已由代码强制补齐")

    app_logger.info(f"✅ 分类完成：{len(items)} 个 Agent")
    return {"classifications": items}

def route_to_agents(state: DestinationRouterState) -> list[Send]:
    """路由函数（条件边）：把 classifications 扇出成一批并行任务"""
    sends = []
    seen = set()
    for c in state["classifications"]:
        # ★ 去重：LLM 偶发对同一子查询重复输出（实测"苏州文化探索"出现两次），
        #   按 (agent, query) 建 key、重复的跳过，避免同一分支白跑两遍、报告出现重复段。
        #   .strip() 防"苏州文化探索"和"苏州文化探索 "（尾部空格）被当成两个任务。
        key = (c["agent"], c["query"].strip())
        if key in seen:
            continue
        seen.add(key)
        # Send(目标节点名, 发给该节点的输入)：返回列表里有几个 Send，就并行开几个分支。
        # 发过去的是只含 query/destination 的小 dict（不是整张 State），所以下游节点签名是 state: dict。
        sends.append(Send(c["agent"], {"query": c["query"], "destination": state["destination"]}))

    app_logger.info(f"并行发送 {len(sends)} 个任务（去重后）")
    return sends


from app.tools.rag_tools import get_rag_tools  # 4.2就补上了现在报错是因为没有创建


# 创建探索 Agent（带 RAG 工具）
def _create_explore_agent():
    """创建带 RAG 工具的探索 Agent"""

    llm = ChatTongyi(
        model=settings.qwen_model_name,
        api_key=settings.dashscope_api_key,
        temperature=0.7
    )

    # 获取 RAG 工具
    rag_tools = get_rag_tools()

    # 创建 Agent - Agent 会自主决定调用哪些工具
    agent = create_agent(
        model=llm,
        tools=rag_tools,
        system_prompt="""你是一位专业的旅行顾问，负责为用户提供目的地的详细信息。
            你有以下工具可以使用：
            - search_destination_guide: 检索景点攻略、门票、游玩建议
            - search_food_recommendations: 检索美食推荐
            - search_accommodation_info: 检索住宿建议
            - search_travel_tips: 检索旅行注意事项

            **工作方式**：
            1. 分析用户的查询需求
            2. 根据需要选择合适的工具进行检索
            3. 你可以调用多个工具来获取全面的信息
            4. 基于检索到的信息，生成专业、详细的回答

            **注意**：
            - 只有当你需要知识库中的信息时才调用工具
            - 如果用户只是闲聊或问简单问题，直接回答即可
            - 整合多个工具的结果时，注意信息的逻辑性和连贯性
            """
    )
    return agent


_explore_agent = None  # 模块级单例：Agent 造一次就够，别每次进节点都重造


async def explore_agent_node(state: dict) -> dict:
    """探索 Agent 节点（并行分支之一）。签名 state: dict —— 收的是 Send 投喂的小 dict"""
    global _explore_agent
    query, destination = state["query"], state["destination"]
    app_logger.info(f"🏛️ 探索 Agent 执行: {destination} - {query}")

    # ★ 关键顺序：先注入城市，再调 Agent。这样 Agent 内部工具检索时，
    #   _format_rag_results 才能按这个城市过滤（见 4.2），否则会"查上海返回西安攻略"。
    from app.tools.rag_tools import set_rag_city
    set_rag_city(destination)

    if _explore_agent is None:  # 懒加载：第一次用到才造（见 4.3）
        _explore_agent = _create_explore_agent()

    user_message = f"请为我提供关于 {destination} 的以下信息：{query}"
    response = await _explore_agent.ainvoke({"messages": [{"role": "user", "content": user_message}]})
    final_message = response["messages"][-1].content  # 取 Agent 调完工具、整合后的最后一句回复

    formatted = f"## {destination} 旅游信息\n\n{final_message}\n\n---\n*信息来源：知识库检索*\n"
    return {"agent_results": [{"agent_name": "explore", "result": formatted}]}  # 只交自己这条，reducer 会合并

class WeatherCast(BaseModel):
    """单日预报（字段与高德 casts 对齐；缺字段给默认空串，不报错、渲染时留空）"""
    date: str = ""
    dayweather: str = ""
    nightweather: str = ""
    daytemp: str = ""
    nighttemp: str = ""
    daywind: str = ""
    daypower: str = ""


class WeatherForecast(BaseModel):
    """天气预报整体结构（第 12 章流式渲染天气会 import 复用它）"""
    city: str = ""
    reporttime: str = ""
    casts: list[WeatherCast] = Field(default_factory=list)


def _normalize_weather_payload(raw) -> dict:
    """★ 归一化漏斗：把 MCP 三种返回形态统一成 dict（第 12 章也会 import 复用）

    历史 bug：MCP adapter 返回的常是"内容块列表" [{'type':'text','text':'{json}'}]，
    旧代码只按 str/dict 解析 → list 分支没接住 → 恒判"天气为空" → 每次都降级。
    接口本来是好的，纯粹解析口径错，所以先在这里把形态抹平。
    """
    if isinstance(raw, list):                   # 形态一：内容块列表 → 把每块的 text 拼起来
        texts = [str(b.get("text") or "") if isinstance(b, dict) else str(getattr(b, "text", "") or "")
                 for b in raw]
        raw = "\n".join(t for t in texts if t).strip()
    if isinstance(raw, str):                     # 形态二：JSON 字符串 → 抠出 {...} 再 loads
        text = raw.strip()
        if not text.startswith("{"):             # 兼容"说明文字 + JSON"混合文本
            start, end = text.find("{"), text.rfind("}")
            text = text[start:end + 1] if 0 <= start < end else text
        raw = json.loads(text)
    return raw if isinstance(raw, dict) else {}  # 形态三：本来就是 dict → 直接用


async def weather_agent_node(state: dict) -> dict:
    """天气 Agent 节点：调真实高德天气；查不到就如实说，绝不编造"""
    destination = state["destination"]
    app_logger.info(f"🌤️ 天气 Agent 执行: {destination}")

    report = f"## {destination} 天气信息\n\n"
    try:
        from app.tools.mcp_tools import get_weather_tools   # 延迟导入，与工具装载解耦
        tools = await get_weather_tools()
        tool = next((t for t in tools if t.name == "get_weather_forecast"), None)
        if tool is None:
            raise RuntimeError("天气工具未加载")

        # 不传 date：该参数只支持未来 4 天内，超出会返回空预报
        payload = _normalize_weather_payload(await tool.ainvoke({"city": destination}))
        if payload.get("error"):
            raise RuntimeError(str(payload["error"]))

        forecast = WeatherForecast.model_validate(payload)   # Pydantic 校验 + 填默认
        if not forecast.casts:
            raise RuntimeError("天气返回为空")

        lines = [f"📅 {c.date}：白天{c.dayweather} {c.nighttemp}~{c.daytemp}°C，"
                 f"{c.daywind}风 {c.daypower} 级" for c in forecast.casts]
        report += "\n".join(lines)
        report += f"\n\n*数据来源：高德天气（发布于 {forecast.reporttime}）*"
    except Exception as e:
        # ★ 铁律：查不到宁可说"未获取到"，也绝不编造天气（编造会被写进行程正文误导用户）
        app_logger.warning(f"天气查询失败（降级不编造）: {destination} | {type(e).__name__}: {e}")
        report += "暂未获取到实时天气，请以当地实际天气为准。"

    return {"agent_results": [{"agent_name": "weather", "result": report}]}

async def synthesizer_node(state: DestinationRouterState) -> dict:
    """综合器节点（扇入）：等所有并行分支跑完，把结果拼成一份报告"""
    app_logger.info("综合 Agent 结果...")
    results = state["agent_results"]           # reducer 已把各分支结果合并到这里
    if not results:
        return {"final_report": "未找到相关信息。"}
    # 简单拼接就够：攻略段 + 天气段各自成块，顺序无所谓（也可换 LLM 做更智能的综合）
    final_report = "\n\n".join(r["result"] for r in results)
    app_logger.info("✅ 综合完成")
    return {"final_report": final_report}


def create_destination_router():
    """组装并编译整张子图 —— 手工建图四步：建图 → 加节点 → 连边 → compile"""
    workflow = StateGraph(DestinationRouterState)   # ① 以 State 为骨架建图

    # ② 加节点：名字 → 处理函数（名字就是 Send / 边里用的"节点名"）
    workflow.add_node("classifier", classifier_node)
    workflow.add_node("explore", explore_agent_node)
    workflow.add_node("weather", weather_agent_node)
    workflow.add_node("synthesizer", synthesizer_node)

    # ③ 连边
    workflow.add_edge(START, "classifier")          # 入口 → 分类器
    workflow.add_conditional_edges(                 # 分类器 → 条件边（route_to_agents 里用 Send 扇出）
        "classifier", route_to_agents, ["explore", "weather"],  # 第三参：声明可能的目标节点
    )
    workflow.add_edge("explore", "synthesizer")     # 两个分支都汇到综合器（扇入）
    workflow.add_edge("weather", "synthesizer")
    workflow.add_edge("synthesizer", END)           # 综合器 → 出口

    app = workflow.compile()                        # ④ 编译成可 ainvoke 的子图
    app_logger.info("✅ 目的地 Router 创建完成")
    return app