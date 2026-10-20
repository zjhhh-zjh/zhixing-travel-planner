"""
意图路由

早期实现：非流式调用 + 正则抠 JSON + try/except 容错。
现在：ChatTongyi.with_structured_output 强类型输出，无解析容错负担。
路由结果仅用于：① 前端 skills 状态帧 ② 是否走规划 Agent 的分流。
"""
from pydantic import BaseModel, Field
from langchain_community.chat_models import ChatTongyi

from app.config import settings
from app.utils.logger import app_logger

# 技能目录（= 前端 skills 帧的可选值，与 travel agent 的工具能力对应）
SKILL_CATALOG = [
    "search_destination_guide(目的地攻略)", "flight_query(航班查询)",
    "train_query(高铁查询)", "hotel_query(酒店住宿)",
    "food_recommendation(美食推荐)", "weather_query(天气查询)",
    "map_navigation(地图导航)", "itinerary_planning(行程规划)",
]


class SelectedSkill(BaseModel):
    """前端 skills 帧的单项结构（契约字段，不得改名）"""
    skill_name: str = Field(description="技能唯一标识，取自技能目录括号前的英文名")
    title: str = Field(description="技能展示标题，中文")
    desc: str = Field(description="一句话说明本轮用该技能做什么，中文")


class RouterResult(BaseModel):
    """路由结果（强类型，替代原手写 JSON）"""
    # ★ description 会被塞进 prompt：它是写给 LLM 的判定指令，不是写给人看的注释
    is_itinerary_planning: bool = Field(description="用户是否在请求生成/修改行程规划")
    inference_log: str = Field(description="意图判断的推导过程，中文，供前端展示")
    skills: list[SelectedSkill] = Field(description="本轮预计用到的技能，闲聊时为空列表")


_ROUTER_PROMPT = (
    "你是旅行助手的意图路由器。判断用户消息是否为行程规划请求，"
    "并从技能目录中选出本轮预计用到的技能。\n"
    f"技能目录：{'、'.join(SKILL_CATALOG)}\n"
    "注意：闲聊、问候不算行程规划，技能列表留空；"
    "但对正在收集需求/制定行程的对话中的回答（如确认、补充信息、"
    "选择目的地等），必须视为行程规划的延续，is_itinerary_planning 应为 true。"
)


async def chat_route(content: str, history: list[str] | None = None) -> dict | BaseModel:
    """路由阶段：意图判断 + 技能预选

    history：最近几轮对话文本（含用户/助手），供短回答（"确认"等）续上
    需求收集流程。此前只看单条消息，导致需求收集被路由到无上下文的闲聊通道。
    """
    prompt = _ROUTER_PROMPT
    if history:
        # ★ 带上最近几条对话：短回答（"确认"）才能被判成规划的延续
        prompt += "\n\n最近对话记录（供上下文参考）：\n" + "\n".join(history)
    prompt += f"\n\n用户消息：{content}"

    llm = ChatTongyi(
        model=settings.qwen_model_name,
        api_key=settings.dashscope_api_key,
        temperature=0,
    )
    # ★ 一行顶四步：schema 进 prompt → 函数调用格式返回 → 自动校验成 RouterResult
    structured = llm.with_structured_output(RouterResult)
    result = await structured.ainvoke(prompt)
    app_logger.info(f"路由结果: planning={result.is_itinerary_planning}, skills={[s.skill_name for s in result.skills]}")
    return result