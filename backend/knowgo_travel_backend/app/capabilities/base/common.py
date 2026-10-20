"""
capabilities 公共助手函数：四类能力的 flow 共用，避免每个包各写一份
"""
from typing import Any, Sequence


async def get_agent_and_state(conversation_id: str) -> tuple[Any, dict, dict]:
    """取该会话的 Agent、config 和当前 State（只读快照）

    四类 flow 每次要读写状态机都要这三步（建 agent → 拼 config → 取快照），
    抽成一个函数后调用方一行搞定：agent, config, state = await get_agent_and_state(conv_id)
    """
    from app.agents.handoffs.travel_agent import create_travel_agent
    agent = await create_travel_agent()
    config = {
        "configurable": {"thread_id": conversation_id},
        "recursion_limit": 60,   # 抬高 LangGraph 默认递归上限（默认 25），避免长回合被掐断
    }
    snap = await agent.aget_state(config)
    return agent, config, dict(snap.values or {})


def duration_to_minutes(text: str) -> int:
    """历时 HH:mm → 分钟（自动模式代选排序用）

    容错：解析失败返回大数，让这条候选在按耗时排序时自然排到最后（既不崩、也不会被误选为最优）
    """
    try:
        hh, _, mm = str(text or "").partition(":")
        return int(hh) * 60 + int(mm)
    except Exception:
        return 10 ** 6


def leg_signature(legs: Sequence[Any]) -> str:
    """一批段 → 段签名字符串（缓存 key 用），如 '北京→天津@2026-10-01|天津→上海@2026-10-02'

    段对象只要有 from_ / to / travel_date 三个属性即可（高铁、航班、自驾的 XxxLeg 都满足），
    所以用鸭子类型接收，不绑定具体类型。
    """
    # ★ 鸭子类型：有 from_ / to / travel_date 三个属性就行
    return "|".join(f"{leg.from_}→{leg.to}@{leg.travel_date}" for leg in legs)