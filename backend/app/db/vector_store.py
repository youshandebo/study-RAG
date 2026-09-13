# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""向量库连接层：Qdrant 客户端封装 + 进程内向量索引兜底。

隔离下沉（重要）
----------------
租户（tenant_id）与课程（course_id）过滤都发生在**数据层**，而不是检索完
再在应用层筛。原因：应用层过滤一旦某个分支漏判就是跨租户数据泄漏，
而数据层 filter 是"根本取不到"，没有漏判空间。

Qdrant 路径用 `must` 硬条件；内存路径用等价的子集收窄。两者语义必须一致，
否则本地能过、上云串租户——`test_tenant_filter_parity` 钉住这个等价性。
"""
from __future__ import annotations

import threading
from typing import Protocol

from app.core.config import get_settings
from app.core.tenancy import DEFAULT_TENANT, multi_tenant_enabled


def _tenant_filter_active(tenant_id: str | None) -> bool:
    """是否施加租户过滤。

    只在多租户模式开启**且**解析出有效租户时施加。单租户模式下不加，
    以保证存量数据（payload 里没有 tenant_id）在默认部署下依然可见——
    否则升级即"数据全丢"，这是不可接受的回归。
    """
    return bool(tenant_id) and multi_tenant_enabled()


class VectorStore(Protocol):
    async def upsert(self, point_id: str, vector: list[float], payload: dict) -> None: ...

    async def search(
        self,
        vector: list[float],
        top_k: int,
        course_id: str | None = None,
        tenant_id: str | None = None,
    ) -> list[tuple[str, float, dict]]: ...


class InMemoryVectorStore:
    """零依赖兜底实现：暴力余弦扫描，教学场景数据量级下完全够用。"""

    def __init__(self) -> None:
        self._points: dict[str, tuple[list[float], dict]] = {}
        self._lock = threading.Lock()

    async def upsert(self, point_id: str, vector: list[float], payload: dict) -> None:
        with self._lock:
            self._points[point_id] = (vector, payload)

    async def search(
        self,
        vector: list[float],
        top_k: int,
        course_id: str | None = None,
        tenant_id: str | None = None,
    ) -> list[tuple[str, float, dict]]:
        from app.services.rag import embedder

        with self._lock:
            items = list(self._points.items())
        # 租户先于课程收窄：租户是最外层边界
        if _tenant_filter_active(tenant_id):
            items = [
                (pid, vp) for pid, vp in items
                if str((vp[1] or {}).get("tenant_id") or DEFAULT_TENANT) == tenant_id
            ]
        if course_id:
            items = [(pid, vp) for pid, vp in items if vp[1].get("course_id") == course_id]
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

    async def search(
        self,
        vector: list[float],
        top_k: int,
        course_id: str | None = None,
        tenant_id: str | None = None,
    ) -> list[tuple[str, float, dict]]:
        from qdrant_client import models

        must: list = []
        # 租户硬过滤：与 course_id 同为 must，任一不满足即取不到，
        # 不存在"查出来再筛"的中间态。
        if _tenant_filter_active(tenant_id):
            must.append(
                models.FieldCondition(
                    key="tenant_id", match=models.MatchValue(value=tenant_id)
                )
            )
        if course_id:
            # Payload Filter：检索强作用域隔离，杜绝跨课程串台
            must.append(
                models.FieldCondition(
                    key="course_id", match=models.MatchValue(value=course_id)
                )
            )
        query_filter = models.Filter(must=must) if must else None

        hits = await self._client.search(
            collection_name=self._collection,
            query_vector=vector,
            limit=top_k,
            query_filter=query_filter,
        )
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
