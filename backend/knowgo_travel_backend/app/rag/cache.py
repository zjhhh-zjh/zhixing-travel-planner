"""
RAG 缓存层
使用 Redis 缓存检索结果
"""
import os
import json
import hashlib
from typing import List, Optional

import redis
from langchain_core.documents import Document

from app.utils.logger import app_logger


class RAGCache:
    """
    RAG 检索结果 Redis 缓存

    key: rag:cache:{md5(query + top_k)}
    value: JSON 序列化的 Document 列表
    TTL: 默认 3600 秒（1 小时）
    """

    def __init__(
            self,
            ttl: int = 3600,
            enabled: bool = True,
    ):
        self.ttl = ttl
        self.enabled = enabled

        if not enabled:
            return

        try:
            self.redis_client = redis.Redis(
                host=os.getenv("REDIS_HOST", "localhost"),
                port=int(os.getenv("REDIS_PORT", 6379)),
                db=int(os.getenv("REDIS_DB", 0)),
                password=os.getenv("REDIS_PASSWORD", None),
                decode_responses=False,   # 用二进制模式（JSON 会先转 bytes）
            )
            # 连通性探测
            self.redis_client.ping()
            app_logger.info("Redis 缓存已启用")
        except Exception as e:
            # Redis 挂了不阻塞主流程——自动降级为无缓存
            app_logger.warning(f"Redis 连接失败，禁用缓存: {e}")
            self.enabled = False

    def _generate_key(self, query: str, top_k: int) -> str:
        """把查询和 K 一起哈希——不同 K 的结果不能互相污染"""
        content = f"{query}__k{top_k}"
        hash_value = hashlib.md5(content.encode()).hexdigest()
        return f"rag:cache:{hash_value}"

    def get(self, query: str, top_k: int) -> Optional[List[Document]]:
        """从缓存读取"""
        if not self.enabled:
            return None

        key = self._generate_key(query, top_k)
        try:
            cached = self.redis_client.get(key)
            if cached:
                app_logger.info(f"命中缓存: {query[:30]}...")
                docs_data = json.loads(cached)
                return [
                    Document(
                        page_content=item["page_content"],
                        metadata=item["metadata"],
                    )
                    for item in docs_data
                ]
        except Exception as e:
            app_logger.error(f"缓存读取失败: {e}")

        return None

    def set(self, query: str, top_k: int, documents: List[Document]):
        """写入缓存"""
        if not self.enabled:
            return

        key = self._generate_key(query, top_k)
        try:
            docs_data = [
                {
                    "page_content": doc.page_content,
                    "metadata": doc.metadata,
                }
                for doc in documents
            ]
            serialized = json.dumps(docs_data, ensure_ascii=False)
            self.redis_client.setex(key, self.ttl, serialized)
            app_logger.debug(f"已缓存: {query[:30]}...")
        except Exception as e:
            app_logger.error(f"缓存写入失败: {e}")