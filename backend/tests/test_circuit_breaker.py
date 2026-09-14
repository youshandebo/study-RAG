# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""LLM 上游熔断与候选降级单元测试。

运行：cd backend && python -m pytest tests/test_circuit_breaker.py -q

覆盖三件真正会出事的事：
1. **跳闸后立刻失败**——不能让每个请求都陪着等满一次超时；
2. **半开只放行探测**——上游刚恢复就被全量流量二次打挂；
3. **绝不降级到演示引擎**——编答案比答不上来危险得多。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.core.circuit import (
    OPEN,
    CircuitBreaker,
    CircuitConfig,
    LLMUnavailable,
    ResilientLLMProvider,
    build,
    reset_all,
    snapshot,
)


class BoomProvider:
    """恒定失败的 Provider。"""

    def __init__(self, fail_at: int | None = None, pieces: list[str] | None = None) -> None:
        self.name = "boom"
        self.fail_at = fail_at          # 吐了几个 piece 之后炸
        self.pieces = pieces or []
        self.calls = 0

    async def stream_chat(self, messages, system=""):
        self.calls += 1
        for i, piece in enumerate(self.pieces):
            # fail_at=0 → 首包前就炸；fail_at=k → 吐了 k 个之后再炸（模拟中断）
            if self.fail_at is not None and i >= self.fail_at:
                raise RuntimeError("upstream exploded")
            yield piece
        raise RuntimeError("upstream exploded")


class OkProvider:
    def __init__(self, pieces: list[str]) -> None:
        self.name = "ok"
        self.pieces = pieces
        self.calls = 0

    async def stream_chat(self, messages, system=""):
        self.calls += 1
        for piece in self.pieces:
            yield piece


def _breaker(threshold: int = 2, cooldown: float = 0.3, half_open: int = 1) -> CircuitBreaker:
    return CircuitBreaker("ut", CircuitConfig(threshold, cooldown, half_open))


@pytest.fixture(autouse=True)
def _clean():
    reset_all()
    yield
    reset_all()


# -------------------------------------------------------------- 状态机 ----
class TestBreaker:
    @pytest.mark.asyncio
    async def test_opens_after_threshold_then_fails_fast(self):
        b = _breaker(threshold=2)
        assert b.allows() is True
        b.record_failure("e1")
        assert b.allows() is True                  # 还没到阈值
        b.record_failure("e2")
        assert b.allows() is False                 # 跳闸：后续请求直接短路
        assert b.state == OPEN

    @pytest.mark.asyncio
    async def test_success_resets(self):
        b = _breaker(threshold=2)
        b.record_failure("e1")
        b.record_success()
        assert b.failures == 0 and b.allows() is True

    @pytest.mark.asyncio
    async def test_half_open_only_admits_probe(self):
        b = _breaker(threshold=1, cooldown=0.05, half_open=1)
        b.record_failure("e1")
        assert b.allows() is False

        import asyncio

        await asyncio.sleep(0.06)
        assert b.state != "closed"
        assert b.allows() is True                  # 冷却到期 → 放行一个探测
        assert b.allows() is False                 # 探测未回来前不放第二个

    @pytest.mark.asyncio
    async def test_probe_success_closes_circuit(self):
        b = _breaker(threshold=1, cooldown=0.05)
        b.record_failure("e1")
        import asyncio

        await asyncio.sleep(0.06)
        b.allows()
        b.record_success()
        assert b.state == "closed" and b.allows() is True

    def test_snapshot_shape(self):
        b = _breaker(threshold=3, cooldown=60)
        b.record_failure("boom")
        s = b.snapshot()
        assert s["label"] == "ut" and s["failures"] == 1 and s["threshold"] == 3
        assert s["last_error"] == "boom"


# ---------------------------------------------------------- 候选降级 ----
class TestResilientProvider:
    @staticmethod
    async def _collect(provider) -> list[str]:
        return [piece async for piece in provider.stream_chat([{"role": "user", "content": "hi"}])]

    @pytest.mark.asyncio
    async def test_switches_to_fallback_before_first_token(self):
        primary = BoomProvider(pieces=[])          # 首包之前就炸
        backup = OkProvider(["好", "的"])
        p = ResilientLLMProvider([build("p", primary), build("b", backup)])

        assert await self._collect(p) == ["好", "的"]
        assert backup.calls == 1

    @pytest.mark.asyncio
    async def test_does_not_switch_after_partial_output(self):
        """已产出内容再换模型会把两个模型的半截答案拼起来——宁可报错。"""
        primary = BoomProvider(fail_at=1, pieces=["半截"])
        p = ResilientLLMProvider([build("p", primary)])

        with pytest.raises(LLMUnavailable):
            await self._collect(p)

    @pytest.mark.asyncio
    async def test_all_candidates_exhausted_raises(self):
        p = ResilientLLMProvider([build("a", BoomProvider()), build("b", BoomProvider())])
        with pytest.raises(LLMUnavailable) as ei:
            await self._collect(p)
        assert "a" in str(ei.value) and "b" in str(ei.value)

    @pytest.mark.asyncio
    async def test_open_candidate_is_skipped_entirely(self):
        b = build("a", BoomProvider())
        b.breaker.record_failure("e1")
        b.breaker.record_failure("e2")            # 阈值 2（默认 3，此处用配置）
        b.breaker.config = CircuitConfig(2, 60.0, 1)
        b.breaker.failures = 2
        b.breaker.opened_at = __import__("time").monotonic()
        backup = OkProvider(["兜底"])

        p = ResilientLLMProvider([b, build("b", backup)])
        assert await self._collect(p) == ["兜底"]
        assert b.provider.calls == 0               # 熔断中的候选一次都没打

    @pytest.mark.asyncio
    async def test_successful_candidate_closes_its_breaker(self):
        cand = build("ut:ok", OkProvider(["x"]))
        p = ResilientLLMProvider([cand])
        await self._collect(p)
        assert cand.breaker.failures == 0

    def test_labels_exposed_for_ops(self):
        p = ResilientLLMProvider([build("a", OkProvider([])), build("b", OkProvider([]))])
        assert p.labels == ["a", "b"]

    def test_snapshot_registry(self):
        build("ut:x", OkProvider([]))
        assert any(s["label"] == "ut:x" for s in snapshot())


# ------------------------------------------------------------ 端到端 ----
class TestProviderWiring:
    def test_mock_provider_is_never_wrapped(self, monkeypatch):
        """演示引擎不参与熔断：它没有上游，也不该被当成"恢复"的对象。"""
        from app.services.llm.provider import MockProvider, _resilient

        m = MockProvider()
        assert _resilient("demo", m) is m

    def test_single_provider_not_wrapped(self, monkeypatch):
        """只有一家供应商时降级无处可去，包一层只是徒增栈深。"""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        import app.services.llm.provider as pv

        monkeypatch.setattr(pv, "_build_from_settings", lambda s: {})
        primary = OkProvider(["x"])
        assert pv._resilient("only", primary) is primary

    def test_timeout_is_configurable(self, monkeypatch):
        from app.services.llm.provider import request_timeout_s

        monkeypatch.delenv("LLM_REQUEST_TIMEOUT_S", raising=False)
        assert request_timeout_s() == 120.0
        monkeypatch.setenv("LLM_REQUEST_TIMEOUT_S", "25")
        assert request_timeout_s() == 25.0
