"""
文档加载与预处理
"""
from pathlib import Path
from typing import List

from langchain_community.document_loaders import DirectoryLoader, TextLoader
from langchain_core.documents import Document

from app.utils.logger import app_logger


class DocumentManager:
    """
    文档管理器
    统一从 data/documents/destinations/ 下加载 .md 目的地攻略
    """

    def __init__(self, base_dir: str = None):
        if base_dir is None:
            # document_loader.py 在 app/rag 里，向上找项目根
            project_root = Path(__file__).parent.parent.parent
            self.base_dir = project_root / "data" / "documents"
        else:
            self.base_dir = Path(base_dir)

    def load_destination_documents(self) -> List[Document]:
        """加载所有目的地文档

        Returns:
            LangChain Document 列表；每个 Document 带 metadata:
              - source: 文件绝对路径（LangChain 自动填）
              - source_type: "destination_guide"
              - category: "destinations"
        """
        destinations_dir = self.base_dir / "destinations"

        if not destinations_dir.exists():
            app_logger.warning(f"目的地文档目录不存在: {destinations_dir}")
            return []

        loader = DirectoryLoader(
            str(destinations_dir),
            glob="**/*.md",
            loader_cls=TextLoader,
            loader_kwargs={
                "encoding": "utf-8",
                "autodetect_encoding": False,
            },
        )

        documents = loader.load()
        app_logger.info(f"加载了 {len(documents)} 个目的地文档")

        # 补一层业务元数据，方便后续按类别过滤
        for doc in documents:
            doc.metadata["source_type"] = "destination_guide"
            doc.metadata["category"] = "destinations"

        return documents