# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""阶段软预算：task_phase_timeout_s 从空转配置接线为可观测的安全预算（单据2）。

为什么是"软预算"而不是"硬超时"
--------------------------------
`task_phase_timeout_s` 自初始提交起就是空转配置（无任何执行侧消费者，仅
心跳间隔在用它的 1/3）。治理它有两个方向：

1. **接线为阶段硬超时**（超了就杀）：❌ 与单据1 的存活心跳直接冲突——心跳
   明明证明协程活着，预算到点却把它杀了，等于制造新的误杀源。且"安全预算
   是 Safety Deadline 不是 Liveness"：它是容量规划的归因信号，不是死亡判据
   （死亡判据属于 `task_zombie_timeout_s` + 收尸机制）。
2. **接线为阶段软预算**（超了就记日志 + 计数，不打断）：✅ 每个阶段推进时
   检查上一阶段实际耗时，超过预算即暴露"哪个阶段在超支"。它回答的是
   "预算定多少才合理"——积累数据后即可把 300s 默认值调成有依据的数字。

契约（实现前为红灯）：
1. `ingest_phase_budget_exceeded_total{phase}` 计数器已声明；
2. 阶段实际耗时 > 预算 → 计一次，phase 标签取**被检查的（上一）阶段名**；
3. 预算内的阶段不计数；
4. 存活心跳（"heartbeat" 相位）不是阶段推进，不参与预算检查。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import app.db.relational as repo
from app.api.v1 import ingest as ingest_api
from app.api.v1.auth import AuthUser
from app.core import metrics
from app.core import task_policy


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "phase_budget.db"))
    saved = (repo._engine, repo._sessionmaker, repo._tables_ready, repo._pg_broken)
    repo._engine, repo._sessionmaker, repo._tables_ready, repo._pg_broken = None, None, False, False
    metrics.reset()
    yield
    try:
        if repo._engine is not None:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(repo._engine.dispose())
            finally:
                loop.close()
    except Exception:
        pass
    metrics.reset()
    repo._engine, repo._sessionmaker, repo._tables_ready, repo._pg_broken = saved


def _user() -> AuthUser:
    return AuthUser("u-1", "a@b.com", "free", anonymous=False)


class _SlowFile:
    filename = "lecture.wav"

    async def read(self) -> bytes:
        return b"fake-audio-bytes"


async def _await_terminal(task_id: str, status: str, timeout_s: float = 5.0) -> dict | None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    record = None
    while loop.time() < deadline:
        record = await repo.get_ingest_task(task_id)
        if record and record["status"] == status:
            return record
        await asyncio.sleep(0.05)
    return record


def _budget_count(phase: str) -> float:
    series = metrics._counters.get("ingest_phase_budget_exceeded_total", {})
    return float(sum(v for k, v in series.items()
                     if dict(k).get("phase") == phase)) if series else 0.0


class TestPhaseSoftBudget:
    @pytest.mark.asyncio
    async def test_overrun_phase_is_counted_not_killed(self, db_env, monkeypatch):
        """超预算的阶段必须被计数（而非硬杀）：归因信号，不是死亡判据。

        场景：预算压到 0.05s，uploading 阶段实际跑 0.3s（超 6 倍）后推进到
        chunking。当前实现：无任何检查，计数恒 0（红灯）。
        """
        real = task_policy.effective()
        monkeypatch.setattr(task_policy, "effective", lambda: {
            **real,
            "task_phase_timeout_s": 0.05,  # 软预算：秒级压缩
            "task_zombie_timeout_s": 60.0,  # 放宽，隔离出"预算"单变量
        })

        async def _two_phases(*, heartbeat=None, **kwargs):
            await heartbeat("uploading")
            await asyncio.sleep(0.3)  # 6 倍于预算
            await heartbeat("chunking")
            return {"chunks_added": 1}

        monkeypatch.setattr(ingest_api, "_ingest_locked", _two_phases)

        out = await ingest_api.submit_ingest_task(
            request="unit-test", user=_user(), session_id="s-budget", media_type="audio",
            file=_SlowFile(), lecture_date="", text_content="", subject="数学",
            course_id="default", chapter="", idempotency_key="budget-over",
        )
        record = await _await_terminal(out["task_id"], "succeeded")

        assert record is not None and record["status"] == "succeeded", (
            "软预算绝不硬杀：任务必须正常跑完——杀死活任务的单套超时"
            "会把误杀提前到预算点，正是本单据定性里明确排除的方向")
        assert _budget_count("uploading") >= 1.0, (
            "uploading 阶段实际耗时超过预算，必须计数曝光："
            "不计数则预算是空转配置，容量规划无从归因")

    @pytest.mark.asyncio
    async def test_within_budget_phase_is_not_counted(self, db_env, monkeypatch):
        """反向契约：预算内的阶段不得误报。"""
        real = task_policy.effective()
        monkeypatch.setattr(task_policy, "effective", lambda: {
            **real,
            "task_phase_timeout_s": 30.0,  # 预算宽裕
        })

        async def _fast(*, heartbeat=None, **kwargs):
            await heartbeat("uploading")
            await asyncio.sleep(0.05)
            await heartbeat("chunking")
            return {"chunks_added": 1}

        monkeypatch.setattr(ingest_api, "_ingest_locked", _fast)

        out = await ingest_api.submit_ingest_task(
            request="unit-test", user=_user(), session_id="s-ok", media_type="audio",
            file=_SlowFile(), lecture_date="", text_content="", subject="数学",
            course_id="default", chapter="", idempotency_key="budget-ok",
        )
        assert await _await_terminal(out["task_id"], "succeeded") is not None

        assert _budget_count("uploading") == 0.0, "预算内的阶段不得误报"
        assert _budget_count("chunking") == 0.0

    def test_budget_counter_is_declared(self):
        """契约：计数器必须声明——"从未超支"≠"埋点坏了"。"""
        assert "ingest_phase_budget_exceeded_total" in metrics.DECLARED_COUNTERS
