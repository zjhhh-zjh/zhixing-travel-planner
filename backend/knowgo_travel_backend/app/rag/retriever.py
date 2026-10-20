"""
混合检索器：BM25 + Dense + RRF 融合
"""
from collections import defaultdict
from typing import List, Tuple

from langchain_core.documents import Document
from langchain_chroma import Chroma
from langchain_community.retrievers import BM25Retriever

from app.utils.logger import app_logger


class AdvancedHybridRetriever:
    """
    高级混合检索器

    职责：
      - 同一份查询同时走 BM25 和 Dense
      - RRF 融合两路结果
      - 支持多查询变体（配合下一节的查询优化）
      - 内置内存级 LRU 缓存（可关）
    """

    def __init__(
            self,
            vectorstore: Chroma,
            documents: List[Document],       # 子块列表（做 BM25 索引用）
            k: int = 5,                       # 最终返回前 K 个
            bm25_weight: float = 0.4,
            dense_weight: float = 0.6,
            use_cache: bool = True,
    ):
        self.vectorstore = vectorstore
        self.documents = documents
        self.k = k
        self.bm25_weight = bm25_weight
        self.dense_weight = dense_weight
        self.use_cache = use_cache

        # 内存缓存（进程内）
        self._cache: dict[str, List[Document]] = {} if use_cache else None

        # 初始化 BM25 索引
        self._init_bm25()

    def _init_bm25(self):
        """初始化 BM25 索引（把子块 tokens 建成倒排索引）"""
        app_logger.info("🔧 初始化 BM25 索引...")
        self.bm25_retriever = BM25Retriever.from_documents(self.documents)
        # BM25 各自先取更多候选，融合后再截到 self.k
        self.bm25_retriever.k = self.k * 2
        app_logger.info("✅ BM25 索引初始化完成")

    # ============== 单路检索 ==============

    def _bm25_search(self, query: str, k: int) -> List[Document]:
        """BM25 检索（关键词）"""
        return self.bm25_retriever.invoke(query)[:k]

    def _dense_search(self, query: str, k: int) -> List[Tuple[Document, float]]:
        """Dense 检索（向量相似度）"""
        results = self.vectorstore.similarity_search_with_score(query, k=k)
        # Chroma 默认返回 L2 距离（越小越相似），转成相似度（越大越相似）
        # 转换公式：sim = 1 / (1 + distance)，把 [0, ∞) 映射到 (0, 1]
        return [(doc, 1 / (1 + distance)) for doc, distance in results]

    # ============== RRF 融合 ==============

    def _rrf_fusion(
            self,
            bm25_results: List[Document],
            dense_results: List[Tuple[Document, float]],
            k: int = 60,
    ) -> List[Document]:
        """
        倒数排名融合

        公式：RRF_score(d) = Σ weight_r * 1/(k + rank_r(d))
        """
        scores: dict[int, float] = defaultdict(float)
        doc_map: dict[int, Document] = {}

        # 用文档前 200 字的 hash 做去重键（防止相同内容不同对象）
        def _doc_key(doc: Document) -> int:
            return hash(doc.page_content[:200])

        # BM25 贡献
        for rank, doc in enumerate(bm25_results, start=1):
            key = _doc_key(doc)
            doc_map[key] = doc
            scores[key] += self.bm25_weight * (1 / (k + rank))

        # Dense 贡献
        for rank, (doc, _) in enumerate(dense_results, start=1):
            key = _doc_key(doc)
            doc_map[key] = doc
            scores[key] += self.dense_weight * (1 / (k + rank))

        # 按融合分数排序
        sorted_ = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return [doc_map[key] for key, _ in sorted_[:self.k]]

    # ============== 对外入口 ==============

    def retrieve(self, query: str, queries: List[str] = None) -> List[Document]:
        """
        混合检索主入口

        Args:
            query: 原始查询（用来查缓存 key）
            queries: 可选的查询变体列表（Multi-Query 生成的多个改写）
                     不传就只用 query 一个

        Returns:
            top-k 子块列表
        """
        if self.use_cache and query in self._cache:
            app_logger.info("命中内存缓存")
            return self._cache[query]

        if queries and len(queries) > 1:
            # ---------- 多变体路径 ----------
            app_logger.info(f"使用 {len(queries)} 个查询变体进行检索")
            all_results: List[Document] = []
            for q in queries:
                all_results.extend(self._single_retrieve(q))

            # 去重（同一子块只保留第一次出现的次序）
            seen = set()
            unique: List[Document] = []
            for doc in all_results:
                key = hash(doc.page_content[:200])
                if key not in seen:
                    seen.add(key)
                    unique.append(doc)
            final_results = unique[:self.k]
        else:
            # ---------- 单查询路径 ----------
            final_results = self._single_retrieve(query)

        if self.use_cache:
            self._cache[query] = final_results

        return final_results

    def _single_retrieve(self, query: str) -> List[Document]:
        """单条查询：BM25 + Dense + RRF"""
        # 各自先取 2*k 个候选（宁多勿少）
        bm25_results = self._bm25_search(query, k=self.k * 2)
        dense_results = self._dense_search(query, k=self.k * 2)
        app_logger.debug(
            f"BM25={len(bm25_results)} / Dense={len(dense_results)}"
        )

        fused = self._rrf_fusion(bm25_results, dense_results)
        app_logger.info(f"混合检索完成，返回 {len(fused)} 个结果")
        return fused