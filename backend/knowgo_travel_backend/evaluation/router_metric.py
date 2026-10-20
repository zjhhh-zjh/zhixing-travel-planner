"""意图路由正确性指标（DeepEval 自定义 Metric）

路由器输出是强类型 RouterResult（planning 布尔 + skills 列表），属分类任务，
用确定性断言评分（对=1 / 错=0），无需 LLM-as-judge：
- planning 是路由的核心分流开关，必须精确匹配期望值；
- skills 预选有模糊性，做宽松断言：expected_skills_any 为空=不检查，
  非空=actual 的技能名与期望列表命中任一即可。
"""
import json

from deepeval.metrics import BaseMetric
from deepeval.test_case import LLMTestCase


class RouterCorrectnessMetric(BaseMetric):
    """意图路由正确性：planning 精确匹配 + skills 宽松命中"""

    def __init__(self, expected_planning: bool, expected_skills_any: list[str] | None = None):
        # 期望的意图分流结果（必查）
        self.expected_planning = expected_planning
        # 期望命中的技能集合（None/空 = 不检查 skills）
        self.expected_skills_any = expected_skills_any or []
        # threshold 必须在构造器内赋值：BaseMetric 默认为 None，
        # assert_test 内部流程调用 is_successful 时外部赋值可能未生效
        self.threshold = 1.0
        self.score = 0.0
        self.reason = ""

    def measure(self, test_case: LLMTestCase) -> float:
        # actual_output 由测试侧序列化的 RouterResult JSON
        try:
            actual = json.loads(test_case.actual_output)
        except (json.JSONDecodeError, TypeError):
            self.score, self.reason = 0.0, "路由输出不是合法 JSON（结构化输出失败）"
            return self.score

        actual_planning = bool(actual.get("is_itinerary_planning"))
        actual_skills = [s.get("skill_name") for s in (actual.get("skills") or [])]

        # ① 意图分流必须精确匹配（闲聊误判规划/规划误判闲聊都是历史 bug 来源）
        if actual_planning != self.expected_planning:  # ★ planning 精确匹配
            self.score = 0.0
            self.reason = f"planning 期望 {self.expected_planning}，实际 {actual_planning}"
            return self.score

        # ② skills 宽松校验：要求命中任一期望技能；未要求则跳过
        if self.expected_skills_any:  # ★ skills 宽松：命中任一即可
            hit = [s for s in actual_skills if s in self.expected_skills_any]
            if not hit:
                self.score = 0.0
                self.reason = (
                    f"skills 期望命中 {self.expected_skills_any} 之一，实际 {actual_skills}"
                )
                return self.score
            self.score, self.reason = 1.0, f"planning 正确，技能命中 {hit}"
            return self.score

        self.score, self.reason = 1.0, f"planning 正确（skills={actual_skills}）"
        return self.score

    async def a_measure(self, test_case: LLMTestCase) -> float:
        return self.measure(test_case)

    def is_successful(self) -> bool:
        return self.score >= self.threshold