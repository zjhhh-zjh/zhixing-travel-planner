"""
父子分块：Advanced RAG 的第一层加强
"""
from typing import List, Tuple, Dict

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.utils.logger import app_logger


class AdvancedParentDocumentSplitter:
    """
    高级父子文档切分器

    职责：
      - 一份文档切两次（父 1000 字 / 子 200 字）
      - 维护 child_id → parent_id 映射
      - 检索到子块后能一键取回对应父块作为上下文
    """

    def __init__(
            self,
            parent_chunk_size: int = 1000,
            parent_chunk_overlap: int = 200,
            child_chunk_size: int = 200,
            child_chunk_overlap: int = 50,
    ):
        # 分隔符按中文习惯：段落 > 换行 > 句号 > 逗号
        separators = ["\n\n", "\n", "。", "，", " ", ""]

        self.parent_splitter = RecursiveCharacterTextSplitter(
            chunk_size=parent_chunk_size,
            chunk_overlap=parent_chunk_overlap,
            separators=separators,
        )
        self.child_splitter = RecursiveCharacterTextSplitter(
            chunk_size=child_chunk_size,
            chunk_overlap=child_chunk_overlap,
            separators=separators,
        )

        # 父子映射表（内存中）
        self.parent_child_map: Dict[str, str] = {}   # child_id → parent_id
        self.parent_docs: Dict[str, Document] = {}    # parent_id → 父块

    def split_documents(
            self,
            documents: List[Document]
    ) -> Tuple[List[Document], List[Document]]:
        """
        切分文档为父块和子块

        Args:
            documents: 原始 Document 列表（比如 5 份目的地攻略）

        Returns:
            (parent_docs, child_docs)
            child_docs 是要送去做 embedding 的
            parent_docs 只是"顺便返回"，方便调用方查看
        """
        parent_docs: List[Document] = []
        child_docs: List[Document] = []

        for doc in documents:
            # ---------- 第 1 步：切成父块 ----------
            parent_chunks = self.parent_splitter.split_documents([doc])

            # ---------- 第 2 步：给每个父块打 id + 存表 ----------
            for i, parent_chunk in enumerate(parent_chunks):
                source_file = doc.metadata.get("source", "unknown")
                parent_id = f"{source_file}__parent_{i}"

                parent_chunk.metadata["parent_id"] = parent_id
                parent_chunk.metadata["chunk_type"] = "parent"

                parent_docs.append(parent_chunk)
                self.parent_docs[parent_id] = parent_chunk

                # ---------- 第 3 步：切当前父块为子块 ----------
                child_chunks = self.child_splitter.split_documents([parent_chunk])

                for j, child_chunk in enumerate(child_chunks):
                    child_id = f"{parent_id}__child_{j}"
                    child_chunk.metadata["child_id"] = child_id
                    child_chunk.metadata["parent_id"] = parent_id
                    child_chunk.metadata["chunk_type"] = "child"

                    child_docs.append(child_chunk)
                    self.parent_child_map[child_id] = parent_id

        app_logger.info(
            f"切分完成: {len(parent_docs)} 个父块 / {len(child_docs)} 个子块 "
            f"(平均每父块 {len(child_docs) / max(len(parent_docs), 1):.1f} 子块)"
        )
        return parent_docs, child_docs

    def get_parent_context(self, child_docs: List[Document]) -> List[Document]:
        """
        根据检索到的子块列表，映射到对应的父块（去重后返回）

        典型用法：
            child_hits = retriever.retrieve(query)         # 拿到子块
            parents = splitter.get_parent_context(child_hits)  # 换成完整上下文
            → 喂给 LLM
        """
        app_logger.info(f"子块映射到父块: {len(child_docs)} 个子块")

        seen_parent_ids = set()
        parent_contexts: List[Document] = []

        for child in child_docs:
            parent_id = child.metadata.get("parent_id")
            if parent_id and parent_id not in seen_parent_ids:
                seen_parent_ids.add(parent_id)
                parent = self.parent_docs.get(parent_id)
                if parent:
                    parent_contexts.append(parent)

        app_logger.info(f"映射到 {len(parent_contexts)} 个父块作为上下文")
        return parent_contexts