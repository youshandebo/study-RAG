# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""ScopedQdrantClient 的调用面必须与**真实** qdrant-client 对齐（红灯先行）。

为什么必须有这份测试
--------------------
既有的 `test_tenant_scope.py` 跑在一个**伪造**的 qdrant_client 模块上：替身对
收到的关键字照单全收。它能证明"租户条件被构造并传下去了"，却证明不了
"真实客户端认这些参数名"。而 qdrant-client 在 1.x 内部就做过两处断裂式变更：

- 删除了 `AsyncQdrantClient.search`，改为 `query_points`
  （返回 `QueryResponse.points` 而非列表）；
- 分片键参数由 `shard_key` 改名为 `shard_key_selector`。

这两处都会让伪造测试全绿、真实调用当场 `AttributeError` / `TypeError`。

所以这里换成**签名守卫替身**：只暴露真实客户端确实存在的方法，并用真实签名
做关键字绑定——方法被删 → AttributeError；参数名不符 → TypeError。
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

qdrant_client = pytest.importorskip("qdrant_client")

from qdrant_client import AsyncQdrantClient  # noqa: E402  真实类，用于取签名

from app.db import vector_store  # noqa: E402

# 各方法需要返回的形状，与真实客户端保持一致（否则断言逻辑本身会失真）
_RESULTS = {
    "query_points": SimpleNamespace(points=[]),
    "search": [],
    "count": SimpleNamespace(count=0),
    "scroll": ([], None),
    "collection_exists": False,
}


class _SignatureGuard:
    """只暴露真实存在的方法，并按真实签名校验关键字。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __getattr__(self, name: str):
        if not hasattr(AsyncQdrantClient, name):
            raise AttributeError(
                f"真实 qdrant_client({qdrant_client.__name__}) 没有 {name} ——"
                "版本演进改名或删除了该方法"
            )
        real = getattr(AsyncQdrantClient, name)
        signature = inspect.signature(real)

        async def _call(*args, **kwargs):
            # 兼容位置参数：`collection_exists(collection_name)` 就是位置调用
            try:
                signature.bind(None, *args, **kwargs)
            except TypeError as exc:
                raise TypeError(f"{name} 的关键字与真实签名不符：{exc}") from exc
            self.calls.append((name, kwargs))
            return _RESULTS.get(name)

        return _call


@pytest.fixture
def scoped(monkeypatch):
    guard = _SignatureGuard()
    monkeypatch.setattr(qdrant_client, "AsyncQdrantClient", lambda *a, **kw: guard)
    client = vector_store.ScopedQdrantClient("http://qdrant:6333", "study")
    return client, guard


class TestCallSurfaceMatchesRealClient:
    @pytest.mark.asyncio
    async def test_search(self, scoped):
        """最致命的一条：真实客户端若已删除 search，这里必须炸出来。"""
        client, guard = scoped
        scope = vector_store.resolve_scope("org-a", base_collection="study")

        await client.search(scope, [0.1, 0.2], 5)
        assert guard.calls and guard.calls[0][0] in ("query_points", "search")

    @pytest.mark.asyncio
    async def test_upsert(self, scoped):
        client, guard = scoped
        scope = vector_store.resolve_scope("org-a", base_collection="study")

        await client.upsert(scope, [("p-1", [0.1, 0.2], {"tenant_id": "org-a"})])
        assert any(name == "upsert" for name, _ in guard.calls)

    @pytest.mark.asyncio
    async def test_count(self, scoped):
        client, guard = scoped
        scope = vector_store.resolve_scope("org-a", base_collection="study")

        await client.count(scope)
        assert any(name == "count" for name, _ in guard.calls)

    @pytest.mark.asyncio
    async def test_delete(self, scoped):
        client, guard = scoped
        scope = vector_store.resolve_scope("org-a", base_collection="study")

        await client.delete(scope, ["p-1"])
        assert any(name == "delete" for name, _ in guard.calls)

    @pytest.mark.asyncio
    async def test_scroll(self, scoped):
        client, guard = scoped
        scope = vector_store.resolve_scope("org-a", base_collection="study")

        await client.scroll_chunks(scope, limit=10)
        assert any(name == "scroll" for name, _ in guard.calls)

    @pytest.mark.asyncio
    async def test_sharded_call_uses_real_param_name(self, scoped, monkeypatch):
        """分片键的真实参数名是 `shard_key_selector`，不是 `shard_key`。"""
        monkeypatch.setenv("MULTI_TENANT_MODE", "1")
        monkeypatch.setenv("QDRANT_TENANT_SHARDING", "1")
        monkeypatch.delenv("QDRANT_DEDICATED_TENANTS", raising=False)

        client, guard = scoped
        scope = vector_store.resolve_scope("org-a", base_collection="study")
        assert scope.shard_key == "org-a", "前置条件：本用例必须走分片路径"

        await client.search(scope, [0.1, 0.2], 5)
        _name, kwargs = guard.calls[0]
        assert "shard_key_selector" in kwargs, (
            "真实客户端的分片键参数名是 shard_key_selector")
