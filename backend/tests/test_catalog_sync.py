# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""多副本索引同步 + 入库回滚补偿（CTO 尽调剩余两项债务）。

两条要钉死的不变量：

1. **落后副本必须自愈**：Worker B 的 BM25 索引在本进程内，Worker A 入库后
   B 必须能通过目录版本号发现并补齐——这是选版本号而非 Pub/Sub 的原因
   （广播会丢，丢了就永久落后，而"索引落后"是持续的错误结果）。
2. **落库失败必须补偿删向量**：否则留下"检索命中、任何列表都查不到"的孤儿切片。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.core.config import get_settings
from app.services.rag import catalog, embedder
from app.services.rag.chunker import Chunk
from app.services.rag.retriever import BM25Index, HybridRetriever


class SharedStore:
    """模拟多副本共享的向量库（Qdrant）：所有 Worker 读写同一份数据。"""

    def __init__(self) -> None:
        self.points: dict[str, tuple[list[float], dict]] = {}
        self.deleted: list[list[str]] = []
        self.upserts = 0
        self.scrolls = 0

    async def upsert(self, point_id: str, vector: list[float], payload: dict) -> None:
        self.upserts += 1
        self.points[point_id] = (vector, payload)

    async def search(self, vector, top_k, course_id=None, tenant_id=None):
        return []

    async def delete(self, point_ids: list[str], tenant_id=None) -> int:
        self.deleted.append(list(point_ids))
        removed = 0
        for pid in point_ids:
            if self.points.pop(pid, None) is not None:
                removed += 1
        return removed

    async def scroll_chunks(self, tenant_id=None, limit: int = 2000):
        self.scrolls += 1
        return [(pid, payload) for pid, (_v, payload) in list(self.points.items())][:limit]


class _VersionSource:
    """可控的目录版本号（替代 Redis：测试里不连真 Redis）。"""

    def __init__(self) -> None:
        self.value = 0
        self.bumps = 0

    def current(self, tenant) -> int:
        return self.value

    def bump(self, tenant) -> int:
        self.bumps += 1
        self.value += 1
        return self.value


@pytest.fixture(autouse=True)
def no_seed(monkeypatch):
    monkeypatch.setenv("SEED_DEMO_CORPUS", "0")
    monkeypatch.setenv("RAG_CATALOG_SYNC_INTERVAL_S", "0")   # 关掉节流，便于断言
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def version_source(monkeypatch):
    src = _VersionSource()
    monkeypatch.setattr(catalog, "current_version", src.current)
    monkeypatch.setattr(catalog, "bump", src.bump)
    return src


def _patch_embed(monkeypatch):
    async def _fake(text: str):
        return [0.5] * 1024, False

    monkeypatch.setattr(embedder, "embed_with_status", _fake)


def _chunk(cid: str, text: str) -> Chunk:
    return Chunk(id=cid, audio_id="a-1", start="00:00", end="00:30", text=text,
                 exam_point="雅可比行列式")


def _worker(store: SharedStore) -> HybridRetriever:
    r = HybridRetriever()
    r._store = store
    return r


# ------------------------------------------------- 跨副本索引同步 ----
class TestCatalogSync:
    @pytest.mark.asyncio
    async def test_lagging_worker_syncs_after_version_bump(self, monkeypatch, version_source):
        """A 入库 → B（另一个进程的副本）下次检索时必须能看到这条切片的关键词索引。"""
        _patch_embed(monkeypatch)
        store = SharedStore()
        worker_a, worker_b = _worker(store), _worker(store)

        await worker_a.register_chunks([_chunk("c1", "雅可比行列式是极坐标换元的缩放因子")])

        assert version_source.bumps == 1, "写路径必须打版本号，否则别的副本永远发现不了"
        assert store.scrolls == 0, "B 还没检索，不该发生重载"

        scored = await worker_b.retrieve_scored("雅可比行列式", top_k=3)

        assert store.scrolls == 1, "版本号变了 → B 应重载一次索引"
        assert [c.id for _s, c in scored] == ["c1"], "B 必须能通过关键词通道召回 A 写入的切片"

    @pytest.mark.asyncio
    async def test_no_version_source_means_no_reload(self, monkeypatch, version_source):
        """无 Redis（单进程）时版本恒为 0 → 不做任何重载，热路径零开销。"""
        _patch_embed(monkeypatch)
        store = SharedStore()
        worker = _worker(store)
        version_source.value = 0

        await worker.retrieve_scored("任何问题", top_k=3)

        assert store.scrolls == 0

    @pytest.mark.asyncio
    async def test_unchanged_version_does_not_reload_twice(self, monkeypatch, version_source):
        _patch_embed(monkeypatch)
        store = SharedStore()
        worker = _worker(store)
        version_source.value = 5

        await worker.retrieve_scored("q1", top_k=3)
        await worker.retrieve_scored("q2", top_k=3)

        assert store.scrolls == 1, "版本号没变就不该再重载（节流之外的第二道保险）"

    @pytest.mark.asyncio
    async def test_sync_refreshes_canonical_flags(self, monkeypatch, version_source):
        """定版是写路径改 payload 的操作：已缓存副本必须刷新权威标志，否则按旧权威打分。"""
        _patch_embed(monkeypatch)
        store = SharedStore()
        worker = _worker(store)
        await worker.register_chunks([_chunk("c1", "雅可比行列式")])
        assert worker._chunks["c1"].is_canonical is False

        payload = dict(store.points["c1"][1])
        payload.update({"is_canonical": True, "method_version": 2})
        store.points["c1"] = (store.points["c1"][0], payload)
        version_source.bump("public")

        await worker.retrieve_scored("雅可比行列式", top_k=3)

        assert worker._chunks["c1"].is_canonical is True
        assert worker._chunks["c1"].method_version == 2


# ------------------------------------------------- 入库回滚补偿 ----
class TestIngestRollback:
    @pytest.mark.asyncio
    async def test_unregister_removes_from_store_and_index(self, monkeypatch, version_source):
        _patch_embed(monkeypatch)
        store = SharedStore()
        worker = _worker(store)
        await worker.register_chunks([_chunk("c1", "雅可比行列式")])

        await worker.unregister_chunks(["c1"], tenants={"public"})

        assert store.deleted == [["c1"]]
        assert "c1" not in worker._chunks and "c1" not in worker._vectors
        assert worker._bm25.scores(embedder.bm25_tokenize("雅可比行列式")).get("c1", 0) == 0

    @pytest.mark.asyncio
    async def test_asset_write_failure_rolls_back_vectors(self, monkeypatch, version_source):
        """核心断言：落库失败时向量必须被删掉，异常继续向上抛（不吞错）。"""
        _patch_embed(monkeypatch)
        import app.db.relational as repo
        from app.api.v1 import ingest as ingest_api

        store = SharedStore()
        worker = _worker(store)
        monkeypatch.setattr(ingest_api, "get_retriever", lambda: _awaitable(worker))

        async def _boom(session_id, meta):
            raise RuntimeError("关系库写入失败")

        monkeypatch.setattr(repo, "add_asset", _boom)
        chunks = [_chunk("c1", "雅可比行列式"), _chunk("c2", "极坐标换元")]

        with pytest.raises(RuntimeError):
            await ingest_api._register_with_rollback(worker, chunks, "s-1", {"kind": "text"})

        assert store.deleted == [["c1", "c2"]], "必须补偿删除刚写入的向量"
        assert "c1" not in worker._chunks and "c2" not in worker._chunks
        assert not worker._bm25.scores(embedder.bm25_tokenize("雅可比行列式"))

    @pytest.mark.asyncio
    async def test_success_path_keeps_chunks(self, monkeypatch, version_source):
        _patch_embed(monkeypatch)
        import app.db.relational as repo
        from app.api.v1 import ingest as ingest_api

        store = SharedStore()
        worker = _worker(store)

        async def _ok(session_id, meta):
            assert meta["chunk_count"] == 1, "chunk_count 必须按实际写入量填写"
            return {"id": "asset-1"}

        monkeypatch.setattr(repo, "add_asset", _ok)

        added, asset = await ingest_api._register_with_rollback(
            worker, [_chunk("c1", "雅可比行列式")], "s-1", {"kind": "text"}
        )

        assert added == 1 and asset["id"] == "asset-1"
        assert store.deleted == [] and "c1" in worker._chunks


async def _awaitable(value):
    return value


# ------------------------------------------------- BM25 摘除 ----
class TestBm25Remove:
    def test_remove_clears_postings_and_keeps_avg_len_sane(self):
        """只删 postings 会留下悬空的文档长度 → _avg_len 偏大 → 别的切片打分失真。"""
        idx = BM25Index()
        idx.add("c1", ["梯度", "下降"], tenant_id="public")
        idx.add("c2", ["极坐标", "换元", "雅可比"], tenant_id="public")
        before = idx._avg_len

        removed = idx.remove(["c1"])

        assert removed == 1
        assert idx.scores(["梯度"]) == {}
        assert idx._doc_len.get("c1") is None
        assert idx._avg_len == float(len(["极坐标", "换元", "雅可比"]))
        assert idx._avg_len != before
        # c2 仍然完好
        assert idx.scores(["极坐标"]).get("c2", 0) > 0

    def test_remove_unknown_ids_is_noop(self):
        idx = BM25Index()
        idx.add("c1", ["梯度"], tenant_id="public")
        assert idx.remove(["nope"]) == 0
        assert idx.scores(["梯度"]).get("c1", 0) > 0
