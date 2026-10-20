"""意图路由评测入口

跑法（任选其一）：
- uv run pytest evaluation/test_router.py -v          # 普通 pytest
- deepeval test run evaluation/test_router.py         # DeepEval CLI（可推 Confident 平台）

原理：逐条用例调用真实路由器 chat_route()，把强类型 RouterResult 序列化为
actual_output，交给自定义 RouterCorrectnessMetric 做确定性断言。
"""
import asyncio
import json
import os

# Windows 下 asyncio + ChatTongyi 异步客户端需要 Selector 事件循环（与 app/run.py 一致）
if os.name == "nt":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from deepeval import assert_test
from deepeval.test_case import LLMTestCase

import pytest

from app.agents.routers.chat_router import chat_route
from evaluation.router_cases import CASES
from evaluation.router_metric import RouterCorrectnessMetric


@pytest.mark.parametrize("case", CASES, ids=[c["note"] for c in CASES])
def test_chat_router(case: dict):
    """单条路由用例：真实调用 LLM 路由器并断言 planning/skills 正确性"""
    # 调用被测函数（async → sync 包装；history 供需求收集短回答续接）
    result = asyncio.run(chat_route(case["msg"], history=case["history"] or None))  # ★ 真实调用路由器

    # 强类型结果序列化为 JSON，作为 DeepEval 用例的实际输出
    actual_output = json.dumps({
        "is_itinerary_planning": result.is_itinerary_planning,
        "skills": [s.model_dump() for s in result.skills],
    }, ensure_ascii=False)

    test_case = LLMTestCase(
        input=case["msg"],
        actual_output=actual_output,
    )

    # 自定义确定性指标：planning 精确匹配 + skills 宽松命中（threshold=1 即全对才算过）
    metric = RouterCorrectnessMetric(
        expected_planning=case["planning"],
        expected_skills_any=case["skills"],
    )
    metric.threshold = 1.0
    assert_test(test_case, [metric])