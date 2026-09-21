# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""贯穿式心跳：让"单阶段内部卡得久"不再被收尸误杀（红灯先行）。

为什么这份测试存在（单据1，治本）
--------------------------------
排队饥饿已由 `_queue_heartbeat_loop` 修复（见 test_ingest_queue_heartbeat.py），
但那颗心跳的**生命周期只覆盖 `_acquire_slot`**。进入 `_ingest_locked` 之后，
`updated_at` 只在四个阶段边界（uploading → transcribing → chunking →
indexing）被 `_beat()` 刷新——阶段**内部**的长时间阻塞（大音频转写、批量
Embedding、慢上游）仍然没有任何时间驱动的心跳。

于是同形问题在执行段复活：单阶段阻塞超过 `task_zombie_timeout_s`（eco/standard
档默认 600s）时，一次 /metrics 拉取就会把**还在健康干活**的任务判成僵尸——
流水线随后真实写完向量与资产，却因终态不可逆落不了账，客户端看到
"进程中断，请重新提交" → 重试 → 重复切片。与排队饥饿是同一个缺陷的两副面孔。

修法为什么是"贯穿式心跳"而不是"更多 _beat 边界"
------------------------------------------------
`_beat` 是**阶段推进**时才触发的离散事件，而阶段内部多久推进一次由上游
（ASR/Embedding）说了算，代码侧无法插桩。唯一可靠的存活证明是**时间驱动**
的独立协程：只要流水线任务还在事件循环里活着，它就按
`min(task_phase_timeout_s / 3, 30s)` 周期 touch `updated_at`——与排队心跳、
gate 租约 ttl/3 续约同一口径。心跳判据从"你到过哪些阶段"升级为"你的协程
还活着吗"，慢与死第一次在整个任务生命周期上可区分。

红灯用真实复现而非 mock：把策略压到秒级，`_ingest_locked` 替身内部
`asyncio.sleep` 一整段超过僵尸配额的时间且**一次 _beat 都不发**，期间发生
真实的 /metrics 拉取。当前实现下该任务必被收尸成 failed——这证明阶段边界
心跳罩不住阶段内部；补上贯穿心跳后它必须活到流水线自然结束。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

import app.db.relational as repo
from app.api.v1 import ingest as ingest_api
from app.api.v1.auth import AuthUser
from app.core import metrics
from app.core import task_policy
from app.main import app


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    """每用例独立 SQLite 库 + 干净指标（与 queue_heartbeat 同款骨架）。"""
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "pipeline_heartbeat.db"))
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


class TestLongPhaseIsNotAZombie:
    @pytest.mark.asyncio
    async def test_slow_phase_survives_scrape(self, db_env, monkeypatch):
        """核心红灯：阶段内部长时间阻塞的任务不得被收尸。

        场景对齐生产形态：一个真实存在的慢阶段（模拟转写/向量化在
        `asyncio.sleep` 里等待上游，协程活着、但一次 `_beat` 都没机会发）。
        僵尸配额 1.0s，阻塞 1.3s，期间一次真实 /metrics 拉取。
        当前实现：任务被判死成 failed（阶段边界心跳罩不住内部）。
        修复后：任务活着，且流水线自然跑完落 succeeded。
        """
        real = task_policy.effective()
        monkeypatch.setattr(task_policy, "effective", lambda: {
            **real,
            "ingest_max_concurrency": 2,   # 不排队，隔离出"纯执行段"变量
            "task_zombie_timeout_s": 1.0,  # 生存配额 1s
            "task_phase_timeout_s": 1.5,   # 贯穿心跳间隔 = 1.5/3 = 0.5s
        })

        started = asyncio.Event()
        release = asyncio.Event()

        async def _slow_phase(*, heartbeat=None, **kwargs):
            # 故意不发任何 _beat：对齐最坏情形——阶段内部整段阻塞，
            # 四个边界心跳一个都轮不到。存活证明只能来自贯穿心跳。
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.05)
            return {"chunks_added": 1}

        monkeypatch.setattr(ingest_api, "_ingest_locked", _slow_phase)

        out = await ingest_api.submit_ingest_task(
            request="unit-test", user=_user(), session_id="s-slow", media_type="audio",
            file=_SlowFile(), lecture_date="", text_content="", subject="数学",
            course_id="default", chapter="", idempotency_key="slow-phase",
        )
        await asyncio.wait_for(started.wait(), timeout=5)

        await asyncio.sleep(1.3)  # 超过 1.0s 生存配额，阶段内部零 _beat

        try:
            with TestClient(app) as client:
                assert client.get("/metrics").status_code == 200, "拉取本身不得失败"

            record = await repo.get_ingest_task(out["task_id"])
            assert record is not None and record["status"] == "running", (
                "阶段内部阻塞的活任务不得被收尸：协程还在事件循环里活着，"
                "只是这一个阶段推进得慢。判据必须是'协程活着吗'，"
                "而不是'最近一次阶段边界是多久前'"
            )
        finally:
            # 清理只负责放行流水线、排空后台任务；吞掉一切异常，
            # 不得掩盖上面的契约断言。
            release.set()
            try:
                await asyncio.wait_for(
                    _await_terminal(out["task_id"], "succeeded", 3.0), timeout=4)
            except Exception:
                pass
