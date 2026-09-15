# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""Embedding 断路器测试。

要钉死的行为只有一条：**跳闸之后不再向上游发请求**。
这是断路器唯一不可退让的语义——若哪天有人为了"多试一次"把冷却期绕过，
上游挂掉时每次检索仍会白等两个超时周期，断路器就退化成了日志装饰。
"""
from __future__ import annotations

import pytest

from app.core import circuit
from app.services.rag import embedder


class _FakeRC:
    """替身配置：不落盘、不读环境变量，只回答 embedding 段。

    必须与 `runtime_config` 的**键集**保持一致（`base_url/api_key/model` 恒在，
    未配置时为空串）——否则测试会因为替身不忠实而假失败。
    """

    def __init__(self, cfg: dict | None) -> None:
        self._cfg = cfg or {}

    def effective(self, section: str) -> dict:
        if section != "embedding":
            return {}
        base = {"base_url": "", "api_key": "", "model": ""}
        base.update(self._cfg)
        return base


@pytest.fixture(autouse=True)
def reset_breakers():
    circuit.reset_all()
    yield
    circuit.reset_all()


def _configure(monkeypatch, model: str = "text-embedding-3-small") -> str:
    cfg = {"api_key": "sk-test", "base_url": "https://api.test/v1", "model": model}
    monkeypatch.setattr(embedder, "runtime_config", _FakeRC(cfg))
    return embedder.breaker_label(cfg)


class _Remote:
    """可控的上游替身：记录调用次数，按需失败。"""

    def __init__(self, vector: list[float] | None = None, error: Exception | None = None) -> None:
        self.calls = 0
        self.vector = vector if vector is not None else [0.02] * 768
        self.error = error

    async def __call__(self, cfg: dict, text: str) -> list[float]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.vector)


async def test_no_config_returns_hash_without_calling_remote(monkeypatch):
    monkeypatch.setattr(embedder, "runtime_config", _FakeRC({}))
    remote = _Remote()
    monkeypatch.setattr(embedder, "_embed_remote", remote)

    vector = await embedder.embed("梯度下降")

    assert remote.calls == 0, "未配置接口时不应发起任何网络调用"
    assert len(vector) == 256


async def test_success_returns_remote_vector(monkeypatch):
    _configure(monkeypatch)
    remote = _Remote(vector=[0.5] * 1024)
    monkeypatch.setattr(embedder, "_embed_remote", remote)

    vector = await embedder.embed("学习率衰减")

    assert remote.calls == 1
    assert len(vector) == 1024, "成功路径必须返回真实向量，而不是哈希兜底"


async def test_success_resets_failure_count(monkeypatch):
    label = _configure(monkeypatch)
    failing = _Remote(error=RuntimeError("502 Bad Gateway"))
    monkeypatch.setattr(embedder, "_embed_remote", failing)

    await embedder.embed("a")
    await embedder.embed("b")
    assert circuit.get_breaker(label).failures == 2

    monkeypatch.setattr(embedder, "_embed_remote", _Remote())
    await embedder.embed("c")

    assert circuit.get_breaker(label).failures == 0, "一次成功就应清零，否则抖动会累积到跳闸"


async def test_opens_after_threshold_and_stops_calling_upstream(monkeypatch):
    label = _configure(monkeypatch)
    failing = _Remote(error=RuntimeError("503 Service Unavailable"))
    monkeypatch.setattr(embedder, "_embed_remote", failing)

    for _ in range(3):
        await embedder.embed("触发跳闸")
    assert failing.calls == 3
    assert circuit.get_breaker(label).state == circuit.OPEN

    # 第 4、5 次：冷却期内必须直接降级，不再打上游
    for _ in range(2):
        vector = await embedder.embed("冷却期请求")
        assert len(vector) == 256

    assert failing.calls == 3, "冷却期内仍向上游发请求 = 断路器失效"
    assert circuit.get_breaker(label).state == circuit.OPEN


async def test_half_open_probe_recovers(monkeypatch):
    label = _configure(monkeypatch)
    monkeypatch.setattr(embedder, "_embed_remote", _Remote(error=RuntimeError("504")))
    for _ in range(3):
        await embedder.embed("触发跳闸")

    breaker = circuit.get_breaker(label)
    # 让"冷却已过"这件事**与绝对时钟无关**：opened_at=0 且 cooldown=0 时，
    # `clock() < opened_at + cooldown` 对任何非负时钟都为假 → 判定 HALF_OPEN。
    # 之前只把 opened_at 设成 0、保留 cooldown=60，等价于赌 `time.monotonic() > 60`；
    # 刚启动的容器/CI runner 上 monotonic 可能小于 60，于是仍被当成 OPEN、不放行探测
    # （本机 uptime 大时却会通过——典型的墙钟依赖测试）。
    breaker.opened_at = 0.0
    breaker.config.cooldown_s = 0.0

    remote = _Remote(vector=[0.3] * 768)
    monkeypatch.setattr(embedder, "_embed_remote", remote)
    vector = await embedder.embed("冷却结束后的探测")

    assert remote.calls == 1, "半开状态应放行一次探测"
    assert len(vector) == 768
    assert breaker.state == circuit.CLOSED
    assert breaker.failures == 0


async def test_breakers_isolated_by_model(monkeypatch):
    """换模型就是换故障域：A 模型挂了不该把 B 模型一起熔断。"""
    label_a = _configure(monkeypatch, model="model-a")
    monkeypatch.setattr(embedder, "_embed_remote", _Remote(error=RuntimeError("502")))
    for _ in range(3):
        await embedder.embed("a")
    assert circuit.get_breaker(label_a).state == circuit.OPEN

    _configure(monkeypatch, model="model-b")
    healthy = _Remote(vector=[0.1] * 768)
    monkeypatch.setattr(embedder, "_embed_remote", healthy)
    await embedder.embed("b")

    assert healthy.calls == 1, "B 模型不应被 A 模型的失败连累"


async def test_breaker_state_visible_for_ops(monkeypatch):
    """/admin/ops/circuit 直接读 snapshot()，embedding 通道必须出现在里面。"""
    label = _configure(monkeypatch)
    monkeypatch.setattr(embedder, "_embed_remote", _Remote(error=RuntimeError("502")))
    for _ in range(3):
        await embedder.embed("x")

    rows = {row["label"]: row for row in circuit.snapshot()}
    assert label in rows
    assert rows[label]["state"] == circuit.OPEN
    assert "502" in rows[label]["last_error"]
