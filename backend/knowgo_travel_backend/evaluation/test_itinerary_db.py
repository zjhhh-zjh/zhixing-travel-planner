"""线上行程质检入口

跑法：uv run pytest evaluation/test_itinerary_db.py -v
从 ItineraryPlans 表拉取最近 N 份真实落库行程，逐份跑三维度 GEval；
库里无数据时跳过（不误报）。与 test_itinerary.py 互补：
- test_itinerary.py：静态样本校准裁判判别力（正反样本）；
- test_itinerary_db.py：线上产出行程质量门禁（全 positive 断言）。
"""
import asyncio
import os

# Windows 下 Selector 事件循环（与 app/run.py、其他评测入口一致）
if os.name == "nt":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import pytest

from evaluation.itinerary_db import fetch_itinerary_cases
from evaluation.itinerary_metric import build_itinerary_metrics

# 质检阈值与线上行程数量上限（每份 3 次裁判调用，控制时长）
THRESHOLD = 0.5
MAX_PLANS = 3

# 参数化在收集期执行 DB 查询；无数据则整组跳过
_CASES = fetch_itinerary_cases(limit=MAX_PLANS)  # ★ 收集期就查库


@pytest.mark.skipif(not _CASES, reason="ItineraryPlans 表中无落库行程，跳过线上质检")
@pytest.mark.parametrize("case", _CASES, ids=[c["note"] for c in _CASES])
def test_online_itinerary_quality(case: dict):
    """线上质检：每份真实落库行程三维度必须全部达标"""
    from deepeval.test_case import LLMTestCase

    test_case = LLMTestCase(
        input=case["input"],
        actual_output=case["actual_output"],
    )

    scores = {}
    reasons = []
    # 本入口的源料来自 plan_to_text（结构化清单，无预算字段），
    # 故 require_budget=False——否则"完整性"维度会因恒缺预算而系统性扣分、误报质量不达标
    for metric in build_itinerary_metrics(threshold=THRESHOLD, require_budget=False):
        metric.measure(test_case)
        scores[metric.name] = metric.score
        if metric.score < THRESHOLD:
            reasons.append(f"{metric.name}: {metric.reason}")

    # 线上产出全部按正样本断言：任一维度不达标即质检失败
    low = {k: v for k, v in scores.items() if v < THRESHOLD}
    assert not low, f"落库行程未达质检标准 {low}\n{chr(10).join(reasons)}"