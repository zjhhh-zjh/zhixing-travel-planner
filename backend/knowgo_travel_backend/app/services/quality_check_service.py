"""行程质量在线质检服务

把 evaluation/ 里的评测核心（Qwen 裁判 + 三维度 GEval）收编为业务服务，
供两处复用：
1. generate_plan 落库成功后异步质检（本文件 run_quality_check）；
2. evaluation/ 评测集（保持离线回归能力，依赖方向：评测 → 业务服务）。

三维度标准与历史修复对齐：
- 完整性（行程正文被精简规则挤没）
- 需求匹配度（文化探索风格却推游乐园类问题）
- 真实性（编造票价/景点）
"""
import asyncio

from deepeval.metrics import GEval
from deepeval.test_case import LLMTestCase, LLMTestCaseParams
from langchain_community.chat_models.tongyi import ChatTongyi
from langchain_core.messages import BaseMessage

from app.config import settings
from app.utils.logger import app_logger
from deepeval.models import DeepEvalBaseLLM


class QwenJudge(DeepEvalBaseLLM):
    """用 Qwen 作为 GEval 裁判（项目无 OpenAI key，替代默认 gpt-4o）"""

    def __init__(self, model_name: str | None = None):
        self._model_name = model_name or settings.qwen_model_name
        super().__init__()  # 基类在此自动 self.model = self.load_model()

    def load_model(self) -> ChatTongyi:
        # 裁判温度 0：打分稳定可复现
        return ChatTongyi(
            model=self._model_name,
            temperature=0,  # ★ 裁判温度 0：打分可复现
            dashscope_api_key=settings.dashscope_api_key,
        )

    def generate(self, prompt: str) -> str:
        result: BaseMessage = self.model.invoke(prompt)
        return result.content

    async def a_generate(self, prompt: str) -> str:
        result: BaseMessage = await self.model.ainvoke(prompt)
        return result.content

    def get_model_name(self) -> str:
        return f"qwen-judge({self._model_name})"
    # 注意：不可重写 model property——基类 __init__ 会 self.model = load_model()


# 裁判上下文：INPUT=需求清单，ACTUAL_OUTPUT=行程正文
_PARAMS = [LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT]


# 完整性维度的"预算明细"判据按源料二选一
# 背景：预算明细只存在于**流式 Markdown 正文**里（summarize_budget_tool / _build_budget 拼接），
# 结构化清单 TravelItinerary 只有 title + itinerary_list，**没有预算字段**。
# 离线评测喂的是 Markdown 正文（含预算表）→ 判据成立；
# 在线质检喂的是 plan_to_text 生成的结构化清单（无预算）→ 判据恒不满足，会系统性扣分。
# 因此改为显式开关，而不是让裁判自己去揣摩"这段文本里有没有预算"（确定性优先）。
_BUDGET_CLAUSE = ("2) 正文末尾附有预算明细（交通/住宿/餐饮/门票等构成）。缺任一项即扣分。")  # ★ 离线：源料是含预算表的正文
_BUDGET_CLAUSE_NA = ("《本条不适用》当前待判内容为结构化行程清单，本身不含预算明细，"  # ★ 在线：源料是没有预算字段的清单
                     "因此不得因缺少预算而扣分；只需判断第 1 项（天数与上午/下午/晚上时段）是否完整。")

def build_itinerary_metrics(threshold: float = 0.5, require_budget: bool = True) -> list[GEval]:
    """构造三维度 GEval（每次新建实例，避免 DeepEval 内部状态串扰）

    require_budget：待判源料是否含预算明细
    - True（默认）：源料是模型输出的 Markdown 正文，含预算表 → 严格校验预算完整性（离线评测口径不变）
    - False：源料是 plan_to_text 产出的结构化清单，无预算字段 → 该条判据不适用，不扣分（在线质检）
    """
    judge = QwenJudge()  # ★ 每次都新建，避免 DeepEval 内部状态串扰
    # 时段口径与代码渲染对齐——正文由 _render_itinerary_body 渲染为
    # **上午/中午/下午/晚上** 四个时段 + 每天"小贴士"（schema 强制），旧判据只写三个时段，
    # 判官口径与产物不一致；末日晚上允许只有 1 条（返程），不得据此判为不完整
    completeness = ("判断行程正文是否完整：1) 每天都有『Day X』标题，且当天包含上午/中午/"
                    "下午/晚上四个时段（末日晚上可只含返程 1 条，不算缺项），"
                    "天数覆盖需求里的全部出行天数；"
                    + (_BUDGET_CLAUSE if require_budget else _BUDGET_CLAUSE_NA))  # ★ 一个开关切两版判据
    return [
        GEval(name="行程完整性",
              criteria=completeness,
              evaluation_params=_PARAMS, model=judge, threshold=threshold),
        GEval(name="需求匹配度",
              criteria=("对照需求清单判断行程是否匹配：目的地正确；每天行程数量与出行天数一致；"
                        "交通方式与需求一致；行程设计体现旅行风格；按人数与预算给出合理的"
                        "餐饮住宿档次。明显偏离即扣分。"),
              evaluation_params=_PARAMS, model=judge, threshold=threshold),
        GEval(name="真实性（无编造）",
              criteria=("判断行程中的景点、餐厅、门票价格是否真实可信：景点应为目的地真实存在"
                        "的知名景点；餐厅应为当地真实特色；门票价格应与常识一致（如灵隐寺约"
                        " 30 元），不应出现明显虚构的名称或离谱价格。发现编造嫌疑即扣分。"),
              evaluation_params=_PARAMS, model=judge, threshold=threshold),
    ]

# ============ 在线质检：落库后异步执行，结果仅日志告警不阻断主流程 ============

# 后台任务引用集合：防止 create_task 的协程被垃圾回收
_BG_TASKS: set = set()  # ★ 强引用后台任务


def plan_to_text(plan_data: dict) -> str:
    """结构化行程 JSON → 裁判可读的行程正文（与模型输出形态对齐）"""
    lines: list[str] = [plan_data.get("title", "行程")]
    for day in plan_data.get("itinerary_list", []):  # ★ 三层键名：itinerary_list → itinerary → scenic_spots_list
        lines.append(f"\n{day.get('day', '')}：")
        for item in day.get("itinerary", []):
            spots = item.get("scenic_spots_list", [])
            parts = [
                f"{s.get('toponym', '')}（{str(s.get('toponym_desc', ''))[:60]}）"
                for s in spots
            ]
            lines.append(f"{item.get('time', '')}：{'、'.join(parts)}")
    return "\n".join(lines)


async def run_quality_check(plan_data: dict, threshold: float = 0.5) -> None:
    """对单份落库行程做三维度质检：通过打 INFO，不通过打 WARNING + 裁判理由

    设计为"事后告警"：行程已落库发给用户，质检不重生成、不阻断；
    异常全吞（fail-safe），绝不影响主对话流程。
    """
    title = plan_data.get("title", "未命名行程")
    try:
        case = LLMTestCase(
            input=title,  # title 由需求生成，是最忠实的需求快照
            actual_output=plan_to_text(plan_data),
        )
        scores: dict[str, float] = {}
        reasons: list[str] = []
        all_reasons: list[str] = []
        # 在线质检的源料是 plan_to_text 产出的结构化清单（无预算字段），
        # 故 require_budget=False——否则"完整性"维度会因恒缺预算而系统性扣分、刷出噪音告警
        for metric in build_itinerary_metrics(threshold=threshold, require_budget=False):  # ★ 在线源料无预算
            await metric.a_measure(case)
            scores[metric.name] = metric.score
            # 所有维度的裁判理由都收集，通过时也记录（便于观察分数波动原因）
            all_reasons.append(f"{metric.name}: {metric.reason}")
            if metric.score < threshold:
                reasons.append(f"{metric.name}: {metric.reason}")

        low = {k: v for k, v in scores.items() if v < threshold}
        if low:
            app_logger.warning(
                f"⚠️ 行程质检未达标: {title} | 分数: {scores} | 问题: {'；'.join(reasons)}"
            )
        else:
            # 通过时同样带裁判理由（截断防日志爆炸），低分维度可追溯
            brief = "；".join(all_reasons)
            if len(brief) > 500:
                brief = brief[:500] + "……"
            app_logger.info(f"✅ 行程质检通过: {title} | 分数: {scores} | 理由: {brief}")
    except Exception as e:
        # 质检自身失败（网络/限流/结构化解析）只记录，不影响业务
        app_logger.warning(f"行程质检执行异常（已忽略）: {title} | {type(e).__name__}: {e}")


def schedule_quality_check(plan_data: dict) -> None:
    """落库后调用：创建后台质检任务（不阻塞回复流）"""
    task = asyncio.create_task(run_quality_check(plan_data))  # ★ 起后台任务，立刻返回
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)