"""
重排序：LLMReranker + LongContextReorder
"""
import os
from typing import List

from langchain_core.documents import Document
from langchain_core.prompts import ChatPromptTemplate
from langchain_community.chat_models import ChatTongyi
from pydantic import BaseModel, Field

from app.utils.logger import app_logger


# 重排也用轻量模型
_rerank_model = ChatTongyi(
    model="qwen-turbo",
    dashscope_api_key=os.getenv("DASHSCOPE_API_KEY"),
    temperature=0,
)


# ============== 结构化输出模型 ==============

class RelevanceScore(BaseModel):
    """单个文档的相关性评分"""
    document_index: int = Field(description="文档索引（从0开始）")
    relevance_score: int = Field(description="相关性得分（0-10）", ge=0, le=10)
    reason: str = Field(description="打分理由（简短）")


class RerankingResult(BaseModel):
    """重排结果"""
    scores: List[RelevanceScore] = Field(description="所有文档的评分列表")


# ============== LLMReranker ==============

class LLMReranker:
    """
    使用 LLM 为每个文档打分，然后按分数重排
    """

    def __init__(self, top_k: int = 3):
        self.top_k = top_k

        self.prompt = ChatPromptTemplate.from_template(
            "你是一个文档相关性评估专家。请评估以下文档与用户查询的相关性。\n\n"
            "用户查询：{query}\n\n"
            "文档列表：\n{documents}\n\n"
            "任务：为每个文档打分（0-10 分），10 分表示完全相关，0 分表示完全不相关。\n\n"
            "评分标准：\n"
            "- 10 分：完美回答查询，信息准确完整\n"
            "- 7-9 分：大部分相关，有用信息较多\n"
            "- 4-6 分：部分相关，有一定参考价值\n"
            "- 1-3 分：相关性很低，几乎没有帮助\n"
            "- 0 分：完全不相关\n\n"
            "请返回一个 JSON 对象，包含每个文档的评分和理由。"
        )

        self.structured_model = _rerank_model.with_structured_output(RerankingResult)

    def rerank(
            self,
            query: str,
            documents: List[Document],
            top_k: int = None,
    ) -> List[Document]:
        """
        重排文档

        Args:
            query: 用户查询
            documents: 待排序的文档列表（一般是混合检索的输出）
            top_k: 返回前 K 个（覆盖构造时的默认值）
        """
        if not documents:
            return []

        top_k = top_k or self.top_k
        app_logger.info(f"开始 LLM 重排 {len(documents)} 个文档...")

        # 把文档格式化成模型能读的字符串
        doc_texts = []
        for i, doc in enumerate(documents):
            content = doc.page_content
            preview = content[:200] + "..." if len(content) > 200 else content
            doc_texts.append(f"[文档 {i}]\n{preview}\n")
        documents_str = "\n".join(doc_texts)

        messages = self.prompt.format_messages(
            query=query,
            documents=documents_str,
        )

        try:
            result: RerankingResult = self.structured_model.invoke(messages)

            # 按分数降序
            sorted_scores = sorted(
                result.scores,
                key=lambda x: x.relevance_score,
                reverse=True,
            )

            reranked_docs: List[Document] = []
            for score_item in sorted_scores[:top_k]:
                idx = score_item.document_index
                if 0 <= idx < len(documents):
                    reranked_docs.append(documents[idx])
                    app_logger.debug(
                        f"  [{idx}] 得分 {score_item.relevance_score}/10 - "
                        f"{score_item.reason}"
                    )

            app_logger.info(f"✅ 重排完成，返回 {len(reranked_docs)} 个文档")
            return reranked_docs

        except Exception as e:
            # 重排失败降级：返回原顺序前 top_k 个
            app_logger.error(f"❌ 重排失败: {e}，返回原始顺序")
            return documents[:top_k]


# ============== LongContextReorder ==============

class LongContextReorder:
    """
    长上下文重排（Lost in the Middle 的解药）

    研究背景：
      模型对上下文**开头 + 结尾**位置的信息记忆最好，
      对**中间位置**的信息容易"迷失"。
    策略：
      - 最相关的文档放在开头
      - 次相关的放在结尾
      - 中间放不太相关的
    """

    @staticmethod
    def reorder(documents: List[Document]) -> List[Document]:
        """
        输入：按相关性降序排列的文档列表 [d1, d2, d3, d4, d5]
        输出：[d1, d3, d5, d4, d2]
                ↑ 最相关放开头
                        ↑ 次相关放结尾
                    ↑ 中间放不太相关的
        """
        if len(documents) <= 2:
            return documents

        app_logger.info(f"🔀 长上下文重排: {len(documents)} 个文档")

        # 分组：偶数索引（相关性排名 1、3、5...）和奇数索引（2、4、6...）
        odd_rank = [d for i, d in enumerate(documents) if i % 2 == 0]
        even_rank = [d for i, d in enumerate(documents) if i % 2 == 1]

        # 偶数排名倒序放后面
        reordered = odd_rank + even_rank[::-1]

        app_logger.debug(
            f"重排后位置：最相关放头 → 不太相关放中 → 次相关放尾"
        )
        return reordered