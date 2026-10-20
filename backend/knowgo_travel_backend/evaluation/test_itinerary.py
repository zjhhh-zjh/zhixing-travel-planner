"""行程质量评测入口

跑法：uv run pytest evaluation/test_itinerary.py -v
（每条样本 × 3 个 GEval 维度，当前 6 条样本 = 18 次 LLM 裁判调用，Qwen 温度 0，约 2 分钟）

双模式断言：
- 正样本（positive=True）：三维度分数全部 ≥ 阈值（行程质量合格）；
- 反样本（positive=False）：至少一个维度分数 < 阈值（裁判能识别出质量问题）。
裁判用的也是 Qwen（温度 0），既评行程也校验裁判本身的判别力。
"""
import asyncio
import os

# Windows 下 Selector 事件循环（与 app/run.py、test_router.py 一致）
if os.name == "nt":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from deepeval.test_case import LLMTestCase

import pytest

from evaluation.itinerary_cases import ITINERARY_CASES
from evaluation.itinerary_metric import build_itinerary_metrics

# 质量达标阈值：GEval 分数 0~1，≥0.5 视为该维度合格
THRESHOLD = 0.5


@pytest.mark.parametrize(
    "case", ITINERARY_CASES, ids=[c["note"] for c in ITINERARY_CASES]
)
def test_itinerary_quality(case: dict):
    """行程质量评测：需求清单 → 行程正文，三维度 GEval 评分后按模式断言"""
    test_case = LLMTestCase(
        input=case["input"],
        actual_output=case["actual_output"],
    )

    # 同步测量三维度分数（GEval 内部调用 Qwen 裁判并给出理由）
    metrics = build_itinerary_metrics(threshold=THRESHOLD)
    scores = {}
    for metric in metrics:
        metric.measure(test_case)
        scores[metric.name] = metric.score

    if case["positive"]:  # ★ 正样本：全部维度达标
        # 正样本：所有维度都必须达标，否则行程质量不合格
        low = {k: v for k, v in scores.items() if v < THRESHOLD}
        assert not low, f"正样本存在不达标维度: {low}（理由: {[m.reason for m in metrics if m.score < THRESHOLD]}）"
    else:
        # 反样本：至少一个维度被裁判打出低分，证明质量问题可被自动识别
        caught = {k: v for k, v in scores.items() if v < THRESHOLD}  # ★ 反样本：至少一个维度被抓住
        assert caught, f"反样本未被裁判识别（全部维度达标）: {scores}"