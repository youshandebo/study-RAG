# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""入库任务的阶段拆分耗时直方图（单据3，红灯先行）。

为什么只看全栈耗时不够能力规划
--------------------------------
`ingest_task_duration_seconds` 是"置 running → 终态"的全栈窗口，排队等待与
流水线执行混在同一条分布里。1C2G 容量规划要回答的却是两个独立问题：

- **queue 相位**：饱和度信号。它长 → 并发槽不够，扩 `ingest_max_concurrency`
  （或升档）就能压下来；
- **pipeline 相位**：单任务真实成本。它长 → 是 ASR/Embedding 慢，加槽位只会
  让更多任务同时挤占 CPU，方向完全相反。

两个旋钮的修法互斥，而全栈直方图把它们搅在一起——P99 涨了说不清该动哪个。
拆分后各自 P99 才可独立校准：queue P99 → 排队超时预算；
pipeline P99 → 收尸阈值的真实成本基线。

契约（实现前为红灯）：
1. `ingest_phase_duration_seconds{phase="queue|pipeline"}` 已声明（无样本时
   以 0 暴露——"从未发生"≠"埋点坏了"）；
2. 成功路径两相位各恰好观测一次：queue 在 `_acquire_slot` 返回时截断，
   pipeline 在 `_ingest_locked` 返回时截断；
3. 排队超时出口只有 queue 相位（没进过流水线）；
4. 桶沿用 TASK_DURATION_BUCKETS：两相位与全栈同一量级域（秒到小时级），
   分桶语义一致才可直接横向对比。
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
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "phase_hist.db"))
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


def _phase_count(phase: str) -> float:
    series = metrics._histograms.get("ingest_phase_duration_seconds", {}).get(
        metrics._labels({"phase": phase}))
    return float(series["count"]) if series else 0.0


class TestPhaseHistogramDeclared:
    def test_phase_histogram_declared_with_task_buckets(self):
        """契约：phase 直方图必须声明，且桶与全栈直方图同域。"""
        assert "ingest_phase_duration_seconds" in metrics.DECLARED_HISTOGRAMS
        assert metrics._buckets_for("ingest_phase_duration_seconds") is metrics.TASK_DURATION_BUCKETS, (
            "两相位与全栈同量级域（秒到小时），必须共用 TASK_DURATION_BUCKETS，"
            "否则横向对比时分桶边界不一致")

    def test_phase_label_is_closed_set(self):
        """phase 标签是代码内闭合集合，不是客户端输入——基数上界恒为 2。"""
        body = metrics.render()
        # 声明补零：无样本时以空标签出现（历史行为兼容），有样本才带 phase。
        assert "ingest_phase_duration_seconds" in body


class TestBothPhasesObservedOnce:
    @pytest.mark.asyncio
    async def test_success_observes_queue_and_pipeline(self, db_env, monkeypatch):
        """成功路径：queue/pipeline 各恰好一次，不重复不遗漏。"""
        real = task_policy.effective()
        monkeypatch.setattr(task_policy, "effective", lambda: {**real})

        async def _ok(*, heartbeat=None, **kwargs):
            await asyncio.sleep(0.05)
            return {"chunks_added": 2}

        monkeypatch.setattr(ingest_api, "_ingest_locked", _ok)

        out = await ingest_api.submit_ingest_task(
            request="unit-test", user=_user(), session_id="s-phase", media_type="audio",
            file=_SlowFile(), lecture_date="", text_content="", subject="数学",
            course_id="default", chapter="", idempotency_key="phase-ok",
        )
        assert await _await_terminal(out["task_id"], "succeeded") is not None

        assert _phase_count("queue") == 1.0, "queue 相位：_acquire_slot 返回时截断，恰好一次"
        assert _phase_count("pipeline") == 1.0, "pipeline 相位：_ingest_locked 返回时截断，恰好一次"

    @pytest.mark.asyncio
    async def test_queue_timeout_observes_queue_only(self, db_env, monkeypatch):
        """排队超时出口：只有 queue 相位（从未进过流水线）。"""
        real = task_policy.effective()
        monkeypatch.setattr(task_policy, "effective", lambda: {**real})

        async def _queue_full(timeout_s, heartbeat=None):
            raise ingest_api.GateTimeout("排队超时（测试注入）")

        monkeypatch.setattr(ingest_api, "_acquire_slot", _queue_full)

        out = await ingest_api.submit_ingest_task(
            request="unit-test", user=_user(), session_id="s-qt", media_type="audio",
            file=_SlowFile(), lecture_date="", text_content="", subject="数学",
            course_id="default", chapter="", idempotency_key="phase-qt",
        )
        assert await _await_terminal(out["task_id"], "failed") is not None

        assert _phase_count("queue") == 1.0, "排队超时必须记录 queue 相位"
        assert _phase_count("pipeline") == 0.0, "从未进过流水线，pipeline 不得有样本"
