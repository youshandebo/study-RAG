# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""LLM 上游熔断与候选降级。

为什么要有这一层
----------------
生产环境里最贵的故障不是"报错"，而是**慢**：上游网关 502 / 机房抖动时，
每个请求都要等满 120 秒超时才失败。连接池被挂满之前，全站问答已经不可用了。
断路器（Circuit Breaker）把"已知对方挂了"这件事显式化：一旦判定故障，
后续请求**立刻失败**并返回明确错误，而不是让每个用户都陪着等一次超时。

状态机
------
    closed ──连续 N 次失败──> open ──冷却到期──> half-open ──成功──> closed
       ^                        │                    │
       └──────── 成功 ←──────────┴────── 再失败 ──────┘

半开状态只放行极少量探测请求：如果上游还没恢复，损失被限制在这几次探测上，
而不是把全量流量重新灌回去（那会让刚恢复的机房二次雪崩）。

绝不降级到 Mock
---------------
模型不可用时不可以退回演示引擎——演示引擎会**编造答案**，
比"答不上来"危险得多（用户会把它当真的）。所以链路走完后抛 `LLMUnavailable`，
由 API 层明确告知"服务暂时不可用"，并且不按正常答案计费。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import AsyncIterator

_logger = logging.getLogger("app.core.circuit")

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """断路器处于 open：本次请求被短路，不要再去连上游。"""


class LLMUnavailable(RuntimeError):
    """所有候选模型都不可用——调用方应给用户明确失败，而不是编答案。"""


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, "").strip() or default))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return max(0.1, float(os.getenv(name, "").strip() or default))
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


@dataclass
class CircuitConfig:
    failure_threshold: int = 3        # 连续失败几次后跳闸
    cooldown_s: float = 60.0          # 跳闸后冷却多久才允许探测
    half_open_max: int = 1            # 半开状态下并发允许的探测数


@dataclass
class CircuitBreaker:
    label: str
    config: CircuitConfig = field(default_factory=CircuitConfig)

    failures: int = 0
    opened_at: float = 0.0
    probes_in_flight: int = 0
    last_error: str = ""

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ------------------------------------------------------------ 状态 ----
    @property
    def state(self) -> str:
        with self._lock:
            if self.failures < self.config.failure_threshold:
                return CLOSED
            return OPEN if time.monotonic() < self.opened_at + self.config.cooldown_s else HALF_OPEN

    def allows(self) -> bool:
        """本次请求是否允许打到上游。"""
        with self._lock:
            if self.failures < self.config.failure_threshold:
                return True
            st = OPEN if time.monotonic() < self.opened_at + self.config.cooldown_s else HALF_OPEN
            if st == OPEN:
                return False
            if self.probes_in_flight < self.config.half_open_max:
                self.probes_in_flight += 1
                return True
            return False

    # ------------------------------------------------------------ 记账 ----
    def record_success(self) -> None:
        with self._lock:
            was_open = self.failures >= self.config.failure_threshold
            self.failures = 0
            self.opened_at = 0.0
            self.probes_in_flight = 0
            self.last_error = ""
        if was_open:
            _logger.info("上游 %s 已恢复，断路器闭合", self.label)

    def record_failure(self, error: str = "") -> None:
        with self._lock:
            self.failures += 1
            self.last_error = str(error)[:300]
            if self.failures >= self.config.failure_threshold:
                self.opened_at = time.monotonic()
            if self.probes_in_flight > 0:
                self.probes_in_flight -= 1
        if self.failures == self.config.failure_threshold:
            _logger.warning(
                "上游 %s 连续失败 %d 次，断路器跳闸（冷却 %.0fs）: %s",
                self.label, self.failures, self.config.cooldown_s, self.last_error,
            )

    def snapshot(self) -> dict:
        with self._lock:
            fails, opened_at, err = self.failures, self.opened_at, self.last_error
        state = self.state
        retry_in = 0.0
        if state == OPEN:
            retry_in = max(0.0, opened_at + self.config.cooldown_s - time.monotonic())
        return {
            "label": self.label,
            "state": state,
            "failures": fails,
            "threshold": self.config.failure_threshold,
            "retry_in_s": round(retry_in, 1),
            "last_error": err,
        }


_BREAKERS: dict[str, CircuitBreaker] = {}
_BREAKERS_LOCK = threading.Lock()


def _default_config() -> CircuitConfig:
    return CircuitConfig(
        failure_threshold=_env_int("LLM_CIRCUIT_THRESHOLD", 3),
        cooldown_s=_env_float("LLM_CIRCUIT_COOLDOWN_S", 60.0),
        half_open_max=_env_int("LLM_CIRCUIT_HALF_OPEN", 1),
    )


def get_breaker(label: str) -> CircuitBreaker:
    """按 (提供商 + 模型) 维度隔离：某条链路炸了不该连累其他链路。"""
    with _BREAKERS_LOCK:
        b = _BREAKERS.get(label)
        if b is None:
            b = CircuitBreaker(label=label, config=_default_config())
            _BREAKERS[label] = b
        return b


def fallback_enabled() -> bool:
    """候选降级开关：默认开。

    关掉的场景：只配了一家供应商（无所谓降级），或部署方要求"宁可报错也不要
    换模型"（模型能力差异会影响答案一致性，某些 SLA 里这是硬要求）。
    """
    return _env_bool("LLM_FALLBACK", True)


def snapshot() -> list[dict]:
    """运营视角：各上游当前状态，供健康检查与管理后台展示。"""
    with _BREAKERS_LOCK:
        return [b.snapshot() for b in _BREAKERS.values()]


def reset_all() -> None:
    """测试用：清空全部断路器状态。"""
    with _BREAKERS_LOCK:
        _BREAKERS.clear()


@dataclass(frozen=True)
class _Candidate:
    label: str
    provider: object          # BaseLLMProvider
    breaker: CircuitBreaker


class ResilientLLMProvider:
    """候选串联 + 每候选独立熔断的流式 Provider。

    只在**第一个 token 之前**才切换到下一个候选：一旦开始产出内容，
    换模型会把两个模型的半截答案拼在一起，那比直接报错更糟。
    """

    name = "resilient"

    def __init__(self, candidates: list[_Candidate]) -> None:
        self._candidates = candidates

    @property
    def labels(self) -> list[str]:
        return [c.label for c in self._candidates]

    async def stream_chat(self, messages: list[dict], system: str = "") -> AsyncIterator[str]:
        errors: list[str] = []
        for cand in self._candidates:
            if not cand.breaker.allows():
                snap = cand.breaker.snapshot()
                errors.append(f"{cand.label}: 熔断中（{snap['retry_in_s']}s 后重试）")
                continue
            started = False
            try:
                async for piece in cand.provider.stream_chat(messages, system):
                    started = True
                    yield piece
                cand.breaker.record_success()
                return
            except Exception as exc:  # noqa: BLE001 - 上游异常类型不可枚举
                cand.breaker.record_failure(f"{type(exc).__name__}: {exc}")
                errors.append(f"{cand.label}: {type(exc).__name__}")
                if started:
                    raise LLMUnavailable(
                        f"{cand.label} 生成中断（已产出内容，不再切换模型以免拼答案）"
                    ) from exc
                continue
        raise LLMUnavailable("全部模型通道不可用： " + " | ".join(errors))

    async def complete(self, messages: list[dict], system: str = "") -> str:
        chunks: list[str] = []
        async for piece in self.stream_chat(messages, system):
            chunks.append(piece)
        return "".join(chunks)


def build(label: str, provider: object) -> _Candidate:
    """构造候选并绑定它自己的断路器。"""
    return _Candidate(label=label, provider=provider, breaker=get_breaker(label))
