"""
查询优化：Multi-Query / HyDE / QueryRewriter
"""
from typing import List

from langchain_core.prompts import ChatPromptTemplate
from langchain_community.chat_models import ChatTongyi

from app.config import settings
from app.utils.logger import app_logger


# 用轻量模型跑查询优化——qwen-turbo 更便宜、更快，效果足够
_optimizer_model = ChatTongyi(
    model="qwen-turbo",
    api_key=settings.dashscope_api_key,
    temperature=0,   # 优化任务要确定性，不要发散
)


# ============== 1. Multi-Query 优化器 ==============

class MultiQueryOptimizer:
    """
    Multi-Query 优化器
    生成 3 个语义相似但表述不同的变体
    """

    def __init__(self, num_variants: int = 3):
        self.num_variants = num_variants
        self.prompt = ChatPromptTemplate.from_template(
            "你是一个查询优化专家。给定一个用户查询，"
            "生成 {num} 个语义相似但表述不同的查询变体。\n\n"
            "原始查询：{query}\n\n"
            "要求：\n"
            "1. 保持原查询的核心意图\n"
            "2. 使用不同的词汇和表述方式\n"
            "3. 考虑同义词、相关概念\n"
            "4. 每行一个变体，不要编号\n\n"
            "变体列表："
        )

    def optimize(self, query: str) -> List[str]:
        """
        Returns:
            [原始查询, 变体1, 变体2, 变体3]
        """
        app_logger.info(f"生成查询变体: {query}")

        messages = self.prompt.format_messages(
            query=query,
            num=self.num_variants,
        )
        response = _optimizer_model.invoke(messages)

        # 按行拆分，过滤空行
        variants = [
            line.strip()
            for line in response.content.strip().split("\n")
            if line.strip()
        ]

        all_queries = [query] + variants[: self.num_variants]

        app_logger.debug(f"生成了 {len(all_queries)} 个变体")
        for i, q in enumerate(all_queries, start=1):
            app_logger.debug(f"  {i}. {q}")

        return all_queries


# ============== 2. HyDE 优化器 ==============

class HyDEOptimizer:
    """
    HyDE (Hypothetical Document Embeddings)
    让 LLM 先生成一段"假想的回答"，用假答案去检索——
    因为答案在词汇上更接近文档本身，检索效果反而更好
    """

    def __init__(self):
        self.prompt = ChatPromptTemplate.from_template(
            "请根据以下问题，生成一段假设性的回答文档。这个文档应该包含可能的答案内容。\n\n"
            "问题：{query}\n\n"
            "要求：\n"
            "1. 200-300 字\n"
            "2. 包含具体的景点名称、特点、推荐理由\n"
            "3. 使用旅游攻略的语言风格\n\n"
            "假设性文档："
        )

    def generate_hypothetical_doc(self, query: str) -> str:
        app_logger.info(f"生成假设性文档: {query}")
        messages = self.prompt.format_messages(query=query)
        response = _optimizer_model.invoke(messages)

        hypothetical_doc = response.content.strip()
        app_logger.debug(f"假设性文档: {hypothetical_doc[:100]}...")
        return hypothetical_doc


# ============== 3. 查询改写器 ==============

class QueryRewriter:
    """
    查询改写器
    修正错别字、口语化 → 规范书面语
    """

    def __init__(self):
        self.prompt = ChatPromptTemplate.from_template(
            "你是一个查询改写专家。请将用户的口语化查询改写为更规范的书面表达。\n\n"
            "原始查询：{query}\n\n"
            "要求：\n"
            "1. 修正错别字\n"
            "2. 将口语转为书面语\n"
            "3. 保持查询意图不变\n"
            "4. 只返回改写后的查询，不要解释\n\n"
            "改写后："
        )

    def rewrite(self, query: str) -> str:
        app_logger.info(f"改写查询: {query}")
        messages = self.prompt.format_messages(query=query)
        response = _optimizer_model.invoke(messages)
        rewritten = response.content.strip()
        app_logger.debug(f"改写结果: {rewritten}")
        return rewritten


# ============== 综合入口 ==============

class AdvancedQueryOptimizer:
    """
    综合查询优化器（可插拔策略）

    strategy 可选：
      - "multi_query": 默认，生成 3 个变体
      - "hyde":        生成假设性文档
      - "rewrite":     只做改写
      - "hybrid":      先改写、再多变体
    """

    def __init__(self, strategy: str = "multi_query"):
        self.strategy = strategy
        self.multi_query = MultiQueryOptimizer()
        self.hyde = HyDEOptimizer()
        self.rewriter = QueryRewriter()

    def optimize(self, query: str) -> List[str]:
        if self.strategy == "multi_query":
            return self.multi_query.optimize(query)

        if self.strategy == "hyde":
            hypothetical_doc = self.hyde.generate_hypothetical_doc(query)
            return [query, hypothetical_doc]

        if self.strategy == "rewrite":
            return [self.rewriter.rewrite(query)]

        if self.strategy == "hybrid":
            rewritten = self.rewriter.rewrite(query)
            return self.multi_query.optimize(rewritten)

        app_logger.warning(f"未知策略 {self.strategy}，使用原始查询")
        return [query]