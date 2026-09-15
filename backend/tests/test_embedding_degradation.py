# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""Embedding 降级期不得污染向量空间（CTO 尽调 must-fix 1）。

要钉死的一条不变量：**降级时绝不把 256 维哈希向量写进主集合**。

这不是"质量略降"的问题：哈希向量与真实 embedding 属于不同向量空间，
写进去的后果是 Qdrant 400、或（集合恰好 256 维时）主空间拓扑被永久污染，
而检索侧 `cosine()` 又会按短维度截断比较，稳定返回错误结果——比"检索不到"更糟。

因此降级期的正确行为是**诚实地退化**：写入侧只进 BM25 稀疏索引，
查询侧只走稀疏通道。本模块把这两条都测死。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.core.config import get_settings
from app.services.rag import embedder
from app.services.rag.chunker import Chunk
from app.services.rag.retriever import HybridRetriever


class SpyStore:
    """记录调用次数的向量库替身——本模块的断言就是"某方法没被调用"。"""

    def __init__(self) -> None:
        self.upserts: list[tuple[str, list[float]]] = []
        self.searches = 0

    async def upsert(self, point_id: str, vector: list[float], payload: dict) -> None:
        self.upserts.append((point_id, vector))

    async def search(self, vector, top_k, course_id=None, tenant_id=None):
        self.searches += 1
        return []


@pytest.fixture(autouse=True)
def no_seed(monkeypatch):
    """关掉演示语料灌入：否则每个用例都会先灌一批种子切片。"""
    monkeypatch.setenv("SEED_DEMO_CORPUS", "0")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _chunk(cid: str, text: str) -> Chunk:
    return Chunk(id=cid, audio_id="a-1", start="00:00", end="00:30", text=text,
                 exam_point="雅可比行列式")


def _retriever() -> tuple[HybridRetriever, SpyStore]:
    r = HybridRetriever()
    store = SpyStore()
    r._store = store
    return r, store


def _patch_embed(monkeypatch, *, degraded: bool, dim: int = 1024):
    async def _fake(text: str):
        return ([0.5] * dim, degraded)

    monkeypatch.setattr(embedder, "embed_with_status", _fake)


# ------------------------------------------------------- 写入侧 ----
@pytest.mark.asyncio
async def test_degraded_write_skips_vector_but_keeps_sparse(monkeypatch):
    _patch_embed(monkeypatch, degraded=True)
    r, store = _retriever()

    added = await r.register_chunks([_chunk("c1", "雅可比行列式是极坐标换元的缩放因子")])

    assert added == 1
    assert store.upserts == [], "降级期绝不允许写向量（会污染主空间）"
    assert "c1" not in r._vectors, "降级向量也不该进进程内向量缓存（同空间假设会被破坏）"
    # 但切片必须仍然可被稀疏通道检索到，不能"降级即丢失"
    assert "c1" in r._chunks
    assert r._bm25.scores(embedder.bm25_tokenize("雅可比行列式")).get("c1", 0) > 0


@pytest.mark.asyncio
async def test_healthy_write_persists_vector(monkeypatch):
    _patch_embed(monkeypatch, degraded=False, dim=1024)
    r, store = _retriever()

    await r.register_chunks([_chunk("c1", "雅可比行列式")])

    assert [p for p, _v in store.upserts] == ["c1"]
    assert len(r._vectors["c1"]) == 1024


# ------------------------------------------------------- 查询侧 ----
@pytest.mark.asyncio
async def test_degraded_query_skips_dense_channel(monkeypatch):
    """降级查询不得走 dense：哈希查询向量与真实集合向量比 cosine 是无意义噪声。"""
    _patch_embed(monkeypatch, degraded=False, dim=1024)
    r, store = _retriever()
    await r.register_chunks([_chunk("c1", "雅可比行列式是极坐标换元的缩放因子")])
    store.searches = 0

    _patch_embed(monkeypatch, degraded=True)
    scored = await r.retrieve_scored("雅可比行列式", top_k=3)

    assert store.searches == 0, "降级查询必须跳过向量通道"
    assert [c.id for _s, c in scored] == ["c1"], "仍应通过 BM25 稀疏通道召回"


@pytest.mark.asyncio
async def test_healthy_query_uses_dense_channel(monkeypatch):
    _patch_embed(monkeypatch, degraded=False, dim=1024)
    r, store = _retriever()
    await r.register_chunks([_chunk("c1", "雅可比行列式")])
    store.searches = 0

    await r.retrieve_scored("雅可比行列式", top_k=3)

    assert store.searches == 1, "正常状态必须走向量通道（否则等于自废 dense 检索）"


# ------------------------------------------------- 降级→恢复 ----
@pytest.mark.asyncio
async def test_recovered_embedding_writes_vectors_again(monkeypatch):
    """降级只是暂时跳过，恢复后新切片必须重新写向量（不能永久退化成 BM25-only）。"""
    _patch_embed(monkeypatch, degraded=True)
    r, store = _retriever()
    await r.register_chunks([_chunk("c1", "降级期入库")])
    assert store.upserts == []

    _patch_embed(monkeypatch, degraded=False, dim=1024)
    await r.register_chunks([_chunk("c2", "恢复后入库")])

    assert [p for p, _v in store.upserts] == ["c2"]
