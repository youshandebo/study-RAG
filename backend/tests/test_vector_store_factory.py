# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""向量库工厂：显式配置必须拒绝静默降级（红灯先行）。

为什么这份测试存在
------------------
`get_vector_store()` 原先在"配了 `QDRANT_URL` 却装不上 `qdrant_client`"时
**静默吞掉 ImportError**，回退到进程内内存库。后果是欺骗性的：

- 编排里明明指向了 `http://qdrant:6333`，实际跑的却是内存库；
- 多副本各存一份、重启即失，检索结果随副本漂移；
- 更关键的是绕过了为 Qdrant 侧精心构造的租户作用域层（失败关闭、
  删除 AND 租户条件等），而内存路径此前连失败关闭都没有。

判据应当是"配置表达了什么意图"：没配 `QDRANT_URL` 是明确的单机/演示意图，
回退内存库合理；**配了**就意味着"我要用 Qdrant"，此时缺包属于部署错误，
必须硬失败让人看见，而不是假装工作。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.db import vector_store


@pytest.fixture(autouse=True)
def _reset_singleton(monkeypatch):
    """工厂是模块级单例：每条用例前清空，否则第一条用例的结果会污染后面。"""
    monkeypatch.setattr(vector_store, "_store", None)


class TestExplicitConfigRefusesSilentDowngrade:
    def test_configured_qdrant_without_client_is_fatal(self, monkeypatch):
        """配了 QDRANT_URL 却缺包 → 硬失败，不得静默退回内存库。"""
        monkeypatch.setattr(vector_store, "get_settings",
                            lambda: type("S", (), {"qdrant_url": "http://qdrant:6333",
                                                   "qdrant_collection": "study"})())

        def _missing_client(*_args, **_kwargs):
            raise ImportError("No module named 'qdrant_client'")

        monkeypatch.setattr(vector_store, "QdrantVectorStore", _missing_client)

        with pytest.raises(RuntimeError) as excinfo:
            vector_store.get_vector_store()
        assert "qdrant-client" in str(excinfo.value), (
            "报错必须指名道姓说出缺哪个包，否则排障者只会看到一堆 500")

    def test_without_qdrant_url_still_uses_memory(self, monkeypatch):
        """未配置 QDRANT_URL 是明确的单机意图，回退内存库不得被误伤。"""
        monkeypatch.setattr(vector_store, "get_settings",
                            lambda: type("S", (), {"qdrant_url": "",
                                                   "qdrant_collection": "study"})())

        store = vector_store.get_vector_store()
        assert isinstance(store, vector_store.InMemoryVectorStore)
