"""
完整的 Advanced RAG 管道
把 5 层（查询优化 → 混合检索 → 重排 → 父块映射 → 长上下文重排）
和缓存串成一条流水线
"""
from typing import List

from langchain_core.documents import Document
from langchain_chroma import Chroma

from app.rag.query_optimizer import AdvancedQueryOptimizer
from app.rag.retriever import AdvancedHybridRetriever
from app.rag.reranker import LLMReranker, LongContextReorder
from app.rag.text_splitter import AdvancedParentDocumentSplitter
from app.rag.cache import RAGCache
from app.utils.logger import app_logger


class AdvancedRAGPipeline:
    """
    Advanced RAG 管道

    对上层就一个 retrieve(query) 方法
    对下面每层可独立换掉（比如把 Multi-Query 换成 HyDE、把 LLM 重排换成 BGE 重排）
    """

    def __init__(
            self,
            vectorstore: Chroma,
            all_documents: List[Document],         # 子块列表（BM25 用）
            parent_splitter: AdvancedParentDocumentSplitter,   # 拿 parent_docs 映射用
            query_strategy: str = "multi_query",
            top_k: int = 3,
            enable_cache: bool = True,
    ):
        self.top_k = top_k

        # 1. 查询优化
        self.query_optimizer = AdvancedQueryOptimizer(strategy=query_strategy)

        # 2. 混合检索（先取 3*k 个子块候选）
        self.retriever = AdvancedHybridRetriever(
            vectorstore=vectorstore,
            documents=all_documents,
            k=top_k * 3,
        )

        # 3. LLM 重排（进一步压到 2*k 个）
        self.reranker = LLMReranker(top_k=top_k * 2)

        # 4. 父块映射器
        self.parent_splitter = parent_splitter

        # 5. 长上下文重排
        self.context_reorder = LongContextReorder()

        # 缓存层
        self.cache = RAGCache(enabled=enable_cache)

    def retrieve(self, query: str) -> List[Document]:
        """
        完整 5 层检索流程

        Args:
            query: 用户查询

        Returns:
            top_k 个父块（已按长上下文策略重排）
        """
        # ---------- 缓存快照 ----------
        cached = self.cache.get(query, self.top_k)
        if cached:
            return cached

        app_logger.info(f"✅ 开始 Advanced RAG 检索: {query}")

        # ---------- 阶段 1：查询优化 ----------
        optimized_queries = self.query_optimizer.optimize(query)
        app_logger.info(f"1. 查询优化：生成 {len(optimized_queries)} 个查询")

        # ---------- 阶段 2：混合检索 ----------
        child_docs = self.retriever.retrieve(
            query=query,
            queries=optimized_queries,
        )
        app_logger.info(f"2. 混合检索：获得 {len(child_docs)} 个子块")

        # ---------- 阶段 3：LLM 重排 ----------
        reranked_child_docs = self.reranker.rerank(
            query=query,
            documents=child_docs,
            top_k=self.top_k * 2,
        )
        app_logger.info(f"3. LLM 重排：保留 {len(reranked_child_docs)} 个子块")

        # ---------- 阶段 4：映射到父块 ----------
        parent_docs = self.parent_splitter.get_parent_context(reranked_child_docs)
        app_logger.info(f"4. 父块映射：获得 {len(parent_docs)} 个完整上下文")

        # ---------- 阶段 5：长上下文重排 ----------
        final_docs = self.context_reorder.reorder(parent_docs[: self.top_k])
        app_logger.info(f"✅ RAG 检索完成，最终返回 {len(final_docs)} 个文档")

        # ---------- 写缓存 ----------
        self.cache.set(query, self.top_k, final_docs)

        return final_docs

# ============== 全局单例 ==============

_pipeline: AdvancedRAGPipeline | None = None


def get_rag_pipeline() -> AdvancedRAGPipeline:
    """获取全局 RAG Pipeline 单例（懒加载）"""
    global _pipeline
    if _pipeline is None:
        from app.rag.document_loader import DocumentManager
        from app.rag.vectorstore import VectorStoreManager

        app_logger.info("首次调用 RAG Pipeline，正在初始化...")

        # 加载文档
        doc_manager = DocumentManager()
        documents = doc_manager.load_destination_documents()

        # 切分（拿到内存里的父子映射）
        splitter = AdvancedParentDocumentSplitter()
        _, child_docs = splitter.split_documents(documents)

        # 加载已有向量库（不重新 embedding）
        vs_manager = VectorStoreManager()
        vectorstore = vs_manager.load_vectorstore()

        _pipeline = AdvancedRAGPipeline(
            vectorstore=vectorstore,
            all_documents=child_docs,
            parent_splitter=splitter,
        )

        app_logger.info("✅ RAG Pipeline 单例已就绪")

    return _pipeline