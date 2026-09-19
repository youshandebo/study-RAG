# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""排队心跳：让"排队饥饿"不再被收尸机制误杀（红灯先行）。

为什么必须补这块
----------------
收尸判的是 running 之后 `updated_at` 的陈旧程度，而 `_acquire_slot` 的排队等待
期间**从不刷新心跳**。在 eco 档（并发 1）下，当前面压着一个 8 分钟的大音频，
后面的任务会在队列里耗光 600s 的生存配额——`DEFAULT_ACQUIRE_TIMEOUT_S` 也恰好
是 600s。于是一次再正常不过的 Scrape，会在任务刚抢到槽位、正要起跑的瞬间把它
打成 failed；随后终态不可逆生效，流水线其实照常跑完并真实写入了向量与资产，
而客户端只看到"任务执行进程中断，请重新提交"→ 重试 → 重复切片。

修法为什么必须是"排队期心跳"而不是"压缩排队超时"
------------------------------------------------
后者会让高负载下的排队任务疯狂抛超时，等于剥夺了任务在队列里安静等待的能力。
真正的判据是：任务所在的协程只要还在事件循环里活着、还在主动等待，它在物理上
就是存活的——这与 gate.py 里分布式租约按 ttl/3 续约是同一口径。
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
    """每用例一套独立 SQLite 库 + 干净指标。"""
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "queue_heartbeat.db"))
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


class TestQueuedTaskIsNotAZombie:
    @pytest.mark.asyncio
    async def test_queued_task_survives_scrape(self, db_env, monkeypatch):
        """核心红灯：排队中的健康任务不得被收尸。

        用真实复现而非 mock：把策略压到秒级，**真实排队 1.4s**（> 1.0s 的生存
        配额），期间发生一次真实的 /metrics 拉取。当前实现下排队阶段不刷心跳，
        任务会被判死；补上排队心跳后必须活下来。

        另一个关键点：占住槽位的任务 A 自带心跳地阻塞——它代表"正在健康推进"
        的任务，测试要确保它也不会 muddy 掉判定（否则两个任务都被收，就分辨
        不出是不是排队误杀了）。
        """
        real = task_policy.effective()
        monkeypatch.setattr(task_policy, "effective", lambda: {
            **real,
            "ingest_max_concurrency": 1,   # 唯一槽位，逼后来者进队列
            "task_zombie_timeout_s": 1.0,  # 生存配额 1s
            "task_phase_timeout_s": 1.5,   # 排队心跳间隔 = 1.5/3 = 0.5s
        })

        occupied = asyncio.Event()
        release = asyncio.Event()

        async def _holding_slot(*, heartbeat=None, **kwargs):
            occupied.set()
            while not release.is_set():
                if heartbeat is not None:
                    await heartbeat("processing")
                await asyncio.sleep(0.05)
            return {"chunks_added": 1}

        monkeypatch.setattr(ingest_api, "_ingest_locked", _holding_slot)

        first = await ingest_api.submit_ingest_task(
            request="unit-test", user=_user(), session_id="s-hb", media_type="audio",
            file=_SlowFile(), lecture_date="", text_content="", subject="数学",
            course_id="default", chapter="", idempotency_key="hb-first",
        )
        await asyncio.wait_for(occupied.wait(), timeout=5)

        queued = await ingest_api.submit_ingest_task(
            request="unit-test", user=_user(), session_id="s-hb", media_type="audio",
            file=_SlowFile(), lecture_date="", text_content="", subject="数学",
            course_id="default", chapter="", idempotency_key="hb-queued",
        )
        await asyncio.sleep(0.3)  # 让它进入 _acquire_slot 的排队等待
        baseline = await repo.get_ingest_task(queued["task_id"])
        assert baseline is not None and baseline["status"] == "running"

        await asyncio.sleep(1.1)  # 累计约 1.4s，已超过 1.0s 的生存配额

        try:
            with TestClient(app) as client:
                assert client.get("/metrics").status_code == 200, "拉取本身不得失败"

            record = await repo.get_ingest_task(queued["task_id"])
            assert record is not None and record["status"] == "running", (
                "排队中的任务不得被收尸：它的协程还在事件循环里活着，"
                "只是还没抢到槽位"
            )
            assert record["updated_at"] > baseline["updated_at"], (
                "排队期间必须持续刷新 updated_at——不刷的话，"
                "'排队很久'与'卡死很久'在数据上完全同形，收尸只能二选一误杀"
            )
        finally:
            # 清理只负责释放槽位、排空后台任务；这里若再抛错会掩盖
            # 上面真正的契约断言，所以一律吞掉。
            release.set()
            for tid in (first["task_id"], queued["task_id"]):
                try:
                    await asyncio.wait_for(_await_terminal(tid, "succeeded", 3.0), timeout=4)
                except Exception:
                    pass
