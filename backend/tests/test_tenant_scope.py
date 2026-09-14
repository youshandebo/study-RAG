# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""租户作用域（TenantScope / ScopedQdrantClient）单元测试。

运行：cd backend && python -m pytest tests/test_tenant_scope.py -q

**为什么要有伪造 Redis 之外的伪造 Qdrant**：本机没装 `qdrant_client`，
若只测降级路径（Memory Store），那"过滤条件到底有没有拼上租户"这条关键断言
就完全测不到——而这恰恰是合规审计问的那个问题。

所以这里用 monkeypatch 把 `qdrant_client` 换成最小可用的替身模块，
让 `FieldCondition / Filter` 的**真实构造逻辑**跑起来，再检查落到
`client.search(**kwargs)` 上的 `query_filter` 里有没有租户条件。
"""
from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


def _install_fake_qdrant(monkeypatch):
    """装配一个够用的 qdrant_client 替身，返回其中的 AsyncQdrantClient 类。"""
    models = types.ModuleType("qdrant_client.models")

    @dataclass
    class FieldCondition:
        key: str = ""
        match: object = None

    @dataclass
    class MatchValue:
        value: object = None

    @dataclass
    class Filter:
        must: list | None = None

    @dataclass
    class VectorParams:
        size: int = 0
        distance: str = ""
        on_disk: bool = False

    @dataclass
    class HnswConfigDiff:
        on_disk: bool = False

    @dataclass
    class PointStruct:
        id: str = ""
        vector: list | None = None
        payload: dict | None = None

    class Distance:
        COSINE = "Cosine"

    class PayloadSchemaType:
        KEYWORD = "keyword"

    for _name, _obj in [
        ("FieldCondition", FieldCondition), ("MatchValue", MatchValue), ("Filter", Filter),
        ("VectorParams", VectorParams), ("HnswConfigDiff", HnswConfigDiff),
        ("PointStruct", PointStruct), ("Distance", Distance),
        ("PayloadSchemaType", PayloadSchemaType),
    ]:
        setattr(models, _name, _obj)

    top = types.ModuleType("qdrant_client")

    class AsyncQdrantClient:
        def __init__(self, url: str = "", **_kw) -> None:
            self.url = url
            self.calls: list[tuple[str, dict]] = []
            self.collections: set[str] = set()
            self.indexes: list[tuple[str, str]] = []
            self.shard_fail = False
            _INSTANCES.append(self)

        async def collection_exists(self, collection_name: str) -> bool:
            return collection_name in self.collections

        async def create_collection(self, **kw):
            self.collections.add(kw.get("collection_name"))
            self.calls.append(("create_collection", kw))

        async def create_payload_index(self, **kw):
            self.indexes.append((kw.get("collection_name"), kw.get("field_name")))
            self.calls.append(("create_payload_index", kw))

        async def search(self, **kw):
            if kw.get("shard_key") and self.shard_fail:
                raise RuntimeError("shard key not found")
            self.calls.append(("search", kw))
            return []

        async def upsert(self, **kw):
            self.calls.append(("upsert", kw))
            return SimpleNamespace(status="completed")

        async def count(self, **kw):
            self.calls.append(("count", kw))
            return SimpleNamespace(count=7)

    top.AsyncQdrantClient = AsyncQdrantClient
    top.models = models
    monkeypatch.setitem(sys.modules, "qdrant_client", top)
    monkeypatch.setitem(sys.modules, "qdrant_client.models", models)
    return AsyncQdrantClient


_INSTANCES: list = []


@pytest.fixture
def fake_qdrant(monkeypatch):
    _INSTANCES.clear()
    return _install_fake_qdrant(monkeypatch)


def _last(kind: str, client):
    return [kw for name, kw in client.calls if name == kind][-1]


def _cond_keys(filter_obj) -> list[str]:
    return [cond.key for cond in (filter_obj.must or [])] if filter_obj else []


# ------------------------------------------------------------ 作用域解析 ----
class TestScopeResolution:
    def test_shared_collection_by_default(self, monkeypatch):
        from app.db.vector_store import resolve_scope

        monkeypatch.delenv("QDRANT_DEDICATED_TENANTS", raising=False)
        monkeypatch.delenv("QDRANT_TENANT_SHARDING", raising=False)
        scope = resolve_scope("org-a", base_collection="study")

        assert scope.collection == "study"
        assert scope.shard_key is None

    def test_dedicated_collection_for_whitelisted_tenant(self, monkeypatch):
        from app.db.vector_store import resolve_scope

        monkeypatch.setenv("QDRANT_DEDICATED_TENANTS", "org-a, org-b")
        assert resolve_scope("org-a", base_collection="study").collection == "tenant_org-a"
        assert resolve_scope("org-c", base_collection="study").collection == "study"

    def test_shard_key_only_when_explicitly_enabled(self, monkeypatch):
        from app.db.vector_store import resolve_scope

        monkeypatch.delenv("QDRANT_TENANT_SHARDING", raising=False)
        assert resolve_scope("org-a", base_collection="study").shard_key is None

        monkeypatch.setenv("QDRANT_TENANT_SHARDING", "1")
        assert resolve_scope("org-a", base_collection="study").shard_key == "org-a"

    def test_active_only_in_multi_tenant_mode(self, monkeypatch):
        from app.db.vector_store import resolve_scope

        monkeypatch.delenv("MULTI_TENANT_MODE", raising=False)
        assert resolve_scope("org-a", base_collection="study").active is False

        monkeypatch.setenv("MULTI_TENANT_MODE", "1")
        assert resolve_scope("org-a", base_collection="study").active is True


# ------------------------------------------------------- 过滤不可绕过 ----
class TestScopedClient:
    def _store(self, monkeypatch, fake_qdrant, **env):
        from app.db.vector_store import QdrantVectorStore

        for k, v in env.items():
            monkeypatch.setenv(k, v)
        return QdrantVectorStore("http://fake:6333", "study")

    @pytest.mark.asyncio
    async def test_tenant_condition_always_present(self, monkeypatch, fake_qdrant):
        store = self._store(monkeypatch, fake_qdrant, MULTI_TENANT_MODE="1")
        await store.search([0.1, 0.2], top_k=5, course_id="c1", tenant_id="org-a")

        kw = _last("search", _INSTANCES[-1])
        assert kw["collection_name"] == "study"
        keys = _cond_keys(kw["query_filter"])
        assert "tenant_id" in keys and "course_id" in keys

    @pytest.mark.asyncio
    async def test_missing_tenant_fails_closed(self, monkeypatch, fake_qdrant):
        """多租户模式下拿不到租户就拒绝，而不是放宽为全库。"""
        from app.db.vector_store import TenantScopeRequired

        store = self._store(monkeypatch, fake_qdrant, MULTI_TENANT_MODE="1")
        with pytest.raises(TenantScopeRequired):
            await store.search([0.1, 0.2], top_k=5, tenant_id=None)
        # 关键：必须**在打到 Qdrant 之前**挡住，而不是查出来再扔掉
        assert [n for n, _ in _INSTANCES[-1].calls if n == "search"] == []

    @pytest.mark.asyncio
    async def test_single_tenant_mode_stays_transparent(self, monkeypatch, fake_qdrant):
        """默认模式下不得有任何过滤——否则存量数据"升级即消失"。"""
        store = self._store(monkeypatch, fake_qdrant)
        monkeypatch.delenv("MULTI_TENANT_MODE", raising=False)
        await store.search([0.1, 0.2], top_k=5, course_id="c1", tenant_id="org-a")

        keys = _cond_keys(_last("search", _INSTANCES[-1])["query_filter"])
        assert "tenant_id" not in keys and "course_id" in keys

    @pytest.mark.asyncio
    async def test_unknown_filter_field_rejected(self, monkeypatch, fake_qdrant):
        """调用方只能传"值"，传不了"条件"：白名单外的字段直接拒绝。"""
        from app.db.vector_store import resolve_scope

        monkeypatch.setenv("MULTI_TENANT_MODE", "1")
        from app.db.vector_store import ScopedQdrantClient, resolve_scope

        scoped = ScopedQdrantClient("http://fake:6333", "study")
        with pytest.raises(ValueError):
            scoped._conditions(resolve_scope("org-a", base_collection="study"), {"user_id": "x"})

    @pytest.mark.asyncio
    async def test_dedicated_tenant_routes_to_own_collection(self, monkeypatch, fake_qdrant):
        store = self._store(
            monkeypatch, fake_qdrant,
            MULTI_TENANT_MODE="1", QDRANT_DEDICATED_TENANTS="org-a",
        )
        await store.search([0.1], top_k=3, tenant_id="org-a")
        assert _last("search", _INSTANCES[-1])["collection_name"] == "tenant_org-a"

    @pytest.mark.asyncio
    async def test_upsert_routes_by_payload_tenant(self, monkeypatch, fake_qdrant):
        store = self._store(
            monkeypatch, fake_qdrant,
            MULTI_TENANT_MODE="1", QDRANT_DEDICATED_TENANTS="org-a",
        )
        await store.upsert("p1", [0.1], {"tenant_id": "org-a", "course_id": "c1"})
        assert _last("upsert", _INSTANCES[-1])["collection_name"] == "tenant_org-a"

    @pytest.mark.asyncio
    async def test_ensure_builds_payload_indexes(self, monkeypatch, fake_qdrant):
        """没有 payload index 的 tenant_id 过滤会退化成全集合扫描。"""
        store = self._store(monkeypatch, fake_qdrant, MULTI_TENANT_MODE="1")
        await store.upsert("p1", [0.1, 0.2, 0.3], {"tenant_id": "org-a"})

        created = {kw["collection_name"] for n, kw in _INSTANCES[-1].calls if n == "create_collection"}
        assert created == {"study"}
        assert ("study", "tenant_id") in _INSTANCES[-1].indexes
        assert ("study", "course_id") in _INSTANCES[-1].indexes

    @pytest.mark.asyncio
    async def test_count_is_tenant_scoped(self, monkeypatch, fake_qdrant):
        store = self._store(monkeypatch, fake_qdrant, MULTI_TENANT_MODE="1")
        assert await store.count(tenant_id="org-a", course_id="c1") == 7
        assert "tenant_id" in _cond_keys(_last("count", _INSTANCES[-1])["count_filter"])


# ------------------------------------------------------------ 分片降级 ----
class TestSharding:
    def _store(self, monkeypatch, fake_qdrant):
        from app.db.vector_store import QdrantVectorStore

        monkeypatch.setenv("MULTI_TENANT_MODE", "1")
        monkeypatch.setenv("QDRANT_TENANT_SHARDING", "1")
        return QdrantVectorStore("http://fake:6333", "study")

    @pytest.mark.asyncio
    async def test_shard_key_passed_when_enabled(self, monkeypatch, fake_qdrant):
        store = self._store(monkeypatch, fake_qdrant)
        await store.search([0.1], top_k=3, tenant_id="org-a")
        assert _last("search", _INSTANCES[-1])["shard_key"] == "org-a"

    @pytest.mark.asyncio
    async def test_degrades_to_payload_filter_when_unsupported(self, monkeypatch, fake_qdrant):
        """旧客户端/非分片集合上报错就该降级，而不是让检索整体挂掉。"""
        store = self._store(monkeypatch, fake_qdrant)
        client = _INSTANCES[-1]
        client.shard_fail = True

        hits = await store.search([0.1], top_k=3, tenant_id="org-a")
        assert hits == []                                  # 降级后仍然出结果
        assert "shard_key" not in _last("search", client)   # 已回退

        await store.search([0.1], top_k=3, tenant_id="org-a")
        assert "shard_key" not in _last("search", client)   # 且不再反复试错


# ------------------------------------------------------ 内存路径等价性 ----
class TestMemoryParity:
    @pytest.mark.asyncio
    async def test_in_memory_respects_scope(self, monkeypatch):
        from app.db.vector_store import InMemoryVectorStore

        monkeypatch.setenv("MULTI_TENANT_MODE", "1")
        store = InMemoryVectorStore()
        await store.upsert("p-a", [1.0, 0.0], {"tenant_id": "org-a"})
        await store.upsert("p-b", [1.0, 0.0], {"tenant_id": "org-b"})

        hits = await store.search([1.0, 0.0], top_k=5, tenant_id="org-a")
        assert [pid for pid, _, _ in hits] == ["p-a"]


# -------------------------------------------------------- 源码结构守卫 ----
class TestSourceGuards:
    def _source(self) -> str:
        return (Path(__file__).resolve().parent.parent / "app" / "db" / "vector_store.py").read_text(encoding="utf-8")

    def test_raw_client_touched_only_inside_scoped_client(self):
        src = self._source()
        head = src.index("class ScopedQdrantClient")
        nxt = src.find("\nclass ", head + 1)
        tail_end = nxt if nxt != -1 else len(src)

        offenders = []
        for lineno, line in enumerate(src.splitlines(), 1):
            if "self._client." not in line:
                continue
            # 行号 → 偏移：只要落在 ScopedQdrantClient 类块之外就是违规
            offset = len("\n".join(src.splitlines()[:lineno - 1])) + (1 if lineno > 1 else 0)
            if not (head <= offset <= tail_end):
                offenders.append(f"{lineno}: {line.strip()}")
        assert not offenders, f"ScopedQdrantClient 之外触碰了裸客户端: {offenders}"

    def test_no_other_module_constructs_qdrant_client(self):
        """隔离只能有一个出口：别的文件 import / 构造 Qdrant 客户端就是绕过作用域。

        只盯真实的 import 与构造语句——配置字段（`qdrant_url`）与 docstring 里
        提到 Qdrant 是允许的，也确实存在。
        """
        app_root = Path(__file__).resolve().parent.parent / "app"
        patterns = ("import qdrant_client", "from qdrant_client", "AsyncQdrantClient", "QdrantClient(")
        offenders: list[str] = []
        for py in app_root.rglob("*.py"):
            if py.name == "vector_store.py":
                continue
            for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith('"""'):
                    continue
                if any(p in line for p in patterns):
                    offenders.append(f"{py.relative_to(app_root)}:{lineno}: {stripped}")
        assert not offenders, f"以下位置绕过了 ScopedQdrantClient: {offenders}"
