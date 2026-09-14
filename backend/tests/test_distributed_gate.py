# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""跨副本并发闸门（DistributedGate）单元测试。

运行：cd backend && python -m pytest tests/test_distributed_gate.py -q

本文件主要验证**降级路径与调用契约**（进程内信号量、参数顺序、超时、心跳生命周期）。
Lua 脚本本身需要真实 Redis 才能执行，属于集成验证范畴，CI 不带 Redis 时跳过。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.core.gate import (
    _ACQUIRE_LUA,
    _HEARTBEAT_LUA,
    _RELEASE_LUA,
    DistributedGate,
    GateTimeout,
)


def _gate(capacity: int = 1, **kw) -> DistributedGate:
    """造一个**确定不使用 Redis** 的闸门：把 URL 显式置空，避免读到开发机环境变量。"""
    return DistributedGate("test", capacity, redis_url="", **kw)


class FakeRedis:
    """记录调用契约的假 Redis：验证 Lua 脚本与参数顺序，不实际执行脚本。"""

    def __init__(self, grant: bool = True, fail: bool = False) -> None:
        self.grant = grant
        self.fail = fail
        self.calls: list[tuple[str, int, tuple]] = []
        self.zset: dict[str, int] = {}

    async def eval(self, script, numkeys, *args):  # noqa: D102 - 测试替身
        self.calls.append((script, numkeys, args))
        if self.fail:
            raise RuntimeError("redis down")
        if script == _ACQUIRE_LUA:
            return 1 if self.grant else 0
        if script == _HEARTBEAT_LUA:
            return 1
        if script == _RELEASE_LUA:
            return 1
        return 0

    async def ping(self):  # noqa: D102 - 测试替身
        return True

    async def zremrangebyscore(self, *args):  # noqa: D102 - 测试替身
        return 0

    async def zcard(self, key):  # noqa: D102 - 测试替身
        return len(self.zset)


# ------------------------------------------------------- 进程内降级 ----
class TestProcessFallback:
    @pytest.mark.asyncio
    async def test_capacity_one_is_mutually_exclusive(self):
        gate = _gate(1)
        assert gate.backend() == "process"

        t1 = await gate.try_acquire()
        assert t1
        assert await gate.try_acquire() is None

        await gate.release(t1)
        assert await gate.try_acquire() is not None

    @pytest.mark.asyncio
    async def test_capacity_n_admit_n(self):
        gate = _gate(2)
        assert await gate.try_acquire() is not None
        assert await gate.try_acquire() is not None
        assert await gate.try_acquire() is None

    @pytest.mark.asyncio
    async def test_release_empty_token_is_noop(self):
        gate = _gate(1)
        await gate.release("")           # 幂等：finally 里可以直接写，不必判空
        assert await gate.try_acquire() is not None

    @pytest.mark.asyncio
    async def test_acquire_waits_until_released(self):
        gate = _gate(1)
        held = await gate.acquire(timeout_s=1.0)

        async def _release_soon():
            await asyncio.sleep(0.15)
            await gate.release(held)

        asyncio.create_task(_release_soon())
        lease = await gate.acquire(timeout_s=2.0)   # 排队后拿到名额
        assert lease

    @pytest.mark.asyncio
    async def test_acquire_times_out_instead_of_hanging(self):
        gate = _gate(1)
        await gate.acquire(timeout_s=1.0)           # 占住唯一名额
        with pytest.raises(GateTimeout):
            await gate.acquire(timeout_s=0.3)

    @pytest.mark.asyncio
    async def test_status_reports_in_flight(self):
        gate = _gate(2)
        st = await gate.status()
        assert st.backend == "process" and st.capacity == 2 and st.in_flight == 0

        await gate.acquire(timeout_s=1.0)
        st = await gate.status()
        assert st.in_flight == 1


# ---------------------------------------------------- Redis 调用契约 ----
class TestRedisContract:
    @pytest.mark.asyncio
    async def test_acquire_passes_expected_arguments(self):
        gate = _gate(2)
        fake = FakeRedis(grant=True)
        gate._redis = fake

        token = await gate.try_acquire()
        assert token

        script, numkeys, args = fake.calls[0]
        key, capacity, ttl_ms, now_ms, sent_token = args
        assert script == _ACQUIRE_LUA and numkeys == 1
        assert key == "gate:test" and capacity == 2
        assert ttl_ms > 0 and now_ms > 0
        assert sent_token == token

    @pytest.mark.asyncio
    async def test_no_capacity_returns_none(self):
        gate = _gate(1)
        gate._redis = FakeRedis(grant=False)
        assert await gate.try_acquire() is None

    @pytest.mark.asyncio
    async def test_redis_failure_degrades_to_process(self):
        """Redis 抖动不能让入库彻底不可用——降级后仍要能拿到名额。"""
        gate = _gate(1)
        gate._redis = FakeRedis(fail=True)

        t1 = await gate.try_acquire()
        assert t1 is not None                     # 降级成功放行
        assert await gate.try_acquire() is None   # 但仍受容量约束，不是无限放行

        await gate.release(t1)

    @pytest.mark.asyncio
    async def test_release_deletes_lease(self):
        gate = _gate(1)
        fake = FakeRedis()
        gate._redis = fake
        token = await gate.try_acquire()

        await gate.release(token)
        script, numkeys, args = fake.calls[-1]
        assert script == _RELEASE_LUA and numkeys == 1
        assert args == ("gate:test", token)


# -------------------------------------------------------------- 心跳 ----
class TestHeartbeat:
    @pytest.mark.asyncio
    async def test_heartbeat_started_and_cancelled(self):
        gate = _gate(1)
        gate._redis = FakeRedis()
        token = await gate.acquire(timeout_s=1.0)  # 心跳只在阻塞版 acquire 里启动
        assert token in gate._beats
        task = gate._beats[token]
        assert not task.done()

        await gate.release(token)
        assert token not in gate._beats
        await asyncio.sleep(0)
        assert task.cancelled() or task.done()

    @pytest.mark.asyncio
    async def test_no_heartbeat_in_process_mode(self):
        gate = _gate(1)
        await gate.acquire(timeout_s=1.0)
        assert gate._beats == {}                   # 进程内信号量无需续约
