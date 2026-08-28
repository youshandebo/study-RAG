# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""向量库连接层：Qdrant 客户端封装 + 进程内向量索引兜底。"""
from __future__ import annotations

import threading
from typing import Protocol

from app.core.config import get_settings


class VectorStore(Protocol):
    async def upsert(self, point_id: str, vector: list[float], payload: dict) -> None: ...

    async def search(self, vector: list[float], top_k: int) -> list[tuple[str, float, dict]]: ...


class InMemoryVectorStore:
    """零依赖兜底实现：暴力余弦扫描，教学场景数据量级下完全够用。"""

    def __init__(self) -> None:
        self._points: dict[str, tuple[list[float], dict]] = {}
        self._lock = threading.Lock()

    async def upsert(self, point_id: str, vector: list[float], payload: dict) -> None:
        with self._lock:
            self._points[point_id] = (vector, payload)

    async def search(self, vector: list[float], top_k: int) -> list[tuple[str, float, dict]]:
        from app.services.rag import embedder

        with self._lock:
            items = list(self._points.items())
        results = [
            (pid, embedder.cosine(vector, vec), payload) for pid, (vec, payload) in items
        ]
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]


class QdrantVectorStore:
    def __init__(self, url: str, collection: str) -> None:
        from qdrant_client import AsyncQdrantClient  # 懒加载可选依赖

        self._client = AsyncQdrantClient(url=url)
        self._collection = collection

    async def ensure_collection(self, dim: int) -> None:
        from qdrant_client import models

        if not await self._client.collection_exists(self._collection):
            await self._client.create_collection(
                collection_name=self._collection,
                vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE),
            )

    async def upsert(self, point_id: str, vector: list[float], payload: dict) -> None:
        from qdrant_client import models

        await self.ensure_collection(len(vector))
        await self._client.upsert(
            collection_name=self._collection,
            points=[models.PointStruct(id=point_id, vector=vector, payload=payload)],
        )

    async def search(self, vector: list[float], top_k: int) -> list[tuple[str, float, dict]]:
        hits = await self._client.search(collection_name=self._collection, query_vector=vector, limit=top_k)
        return [(str(h.id), h.score, h.payload or {}) for h in hits]


_store: VectorStore | None = None


def get_vector_store() -> VectorStore:
    global _store
    if _store is not None:
        return _store
    settings = get_settings()
    if settings.qdrant_url:
        try:
            _store = QdrantVectorStore(settings.qdrant_url, settings.qdrant_collection)
            return _store
        except ImportError:
            pass
    _store = InMemoryVectorStore()
    return _store
