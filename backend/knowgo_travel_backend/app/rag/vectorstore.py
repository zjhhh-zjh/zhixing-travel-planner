"""
向量数据库管理
使用 Chroma + 通义 text-embedding-v2
"""
from pathlib import Path
from typing import List

from langchain_core.documents import Document
from langchain_chroma import Chroma
from langchain_community.embeddings import DashScopeEmbeddings

from app.config import settings
from app.utils.logger import app_logger


class VectorStoreManager:
    """向量数据库管理器"""

    def __init__(
            self,
            persist_directory: str = "data/vectorstore",
            collection_name: str = "travel_guides",
    ):
        self.persist_directory = Path(persist_directory)
        self.collection_name = collection_name

        # 创建目录
        self.persist_directory.mkdir(parents=True, exist_ok=True)

        # Embedding 模型（通义 text-embedding-v2，1536 维）
        self.embeddings = DashScopeEmbeddings(
            model="text-embedding-v2",
            dashscope_api_key=settings.dashscope_api_key,
        )

        self.vectorstore: Chroma | None = None

    def create_vectorstore(self, documents: List[Document]) -> Chroma:
        """
        首次建库：把文档全部 embedding 后写入 Chroma

        Args:
            documents: 通常传子块列表（父块不做检索）
        """
        app_logger.info(f"创建向量数据库（{len(documents)} 个文档）...")

        self.vectorstore = Chroma.from_documents(
            documents=documents,
            embedding=self.embeddings,
            persist_directory=str(self.persist_directory),
            collection_name=self.collection_name,
        )

        app_logger.info("✅ 向量数据库创建完成")
        return self.vectorstore

    def load_vectorstore(self) -> Chroma:
        """
        加载已有向量数据库（不重新 embedding）

        运行时用这个——建库跑一次就够了
        """
        app_logger.info("加载向量数据库...")

        self.vectorstore = Chroma(
            persist_directory=str(self.persist_directory),
            embedding_function=self.embeddings,
            collection_name=self.collection_name,
        )

        app_logger.info("✅ 向量数据库加载完成")
        return self.vectorstore