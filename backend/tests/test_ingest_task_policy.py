# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""入库任务的弹性配置与收尸解耦（红灯先行）。

要证伪的三条现状
----------------
1. **收尸阈值写死**：`ingest._TASK_HEARTBEAT_TIMEOUT_MS` 是常量 30 分钟，与部署
   档位、与管理员配置都无关。弱网环境下大任务要么被误杀（阈值太短），要么
   僵尸停留过久（阈值太长）——两种错法都不可调。
2. **并发槽位一次性定型**：`_ingest_gate()` 是单例，容量在首次调用时取自
   `profiles.effective()["max_concurrent_ingest"]` 后不再变。机器从 1C2G 换到
   8C16G，或接入高性能外部 ASR，资源被软件人为掐死，必须改配置重启。
3. **没有分阶段心跳**：任务进入 running 后 `updated_at` 只在创建/终态时更新。
   一个合法跑 40 分钟的长录音，中途没有任何动作刷新心跳，会被按"无心跳"
   收尸——收尸判据因此无法区分"卡死"与"正在努力算"。

此外还缺一道**保护层**：管理员把并发调到 8 时，若宿主机只剩几十 MB 可用内存，
系统必须自己降级到 1，而不是陪着一起 OOM。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import app.db.relational as repo
from app.api.v1 import ingest as ingest_api
from app.api.v1.auth import AuthUser
from app.core import runtime_config as rc


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    """独立 SQLite 库：任务表与配置写入都不污染开发机。"""
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "task_policy.db"))
    saved = (repo._engine, repo._sessionmaker, repo._tables_ready, repo._pg_broken)
    repo._engine, repo._sessionmaker, repo._tables_ready, repo._pg_broken = None, None, False, False
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
    repo._engine, repo._sessionmaker, repo._tables_ready, repo._pg_broken = saved


def _user() -> AuthUser:
    return AuthUser("u-1", "a@b.com", "free", anonymous=False)


async def _make_task(task_id: str, status: str, updated_at_ms: int) -> None:
    await repo.create_ingest_task({
        "id": task_id,
        "session_id": "s-policy",
        "owner": "u-1",
        "tenant_id": "public",
        "idempotency_key": f"key-{task_id}",
        "media_type": "audio",
        "filename": "lesson.wav",
        "status": status,
        "created_at": updated_at_ms,
        "updated_at": updated_at_ms,
        "finished_at": 0,
        "error": None,
        "result_json": "",
    })


class TestZombieTimeoutIsConfigurable:
    @pytest.mark.asyncio
    async def test_reap_threshold_follows_runtime_config(self, db_env):
        """阈值必须来自配置：配 120s，则 10 分钟无心跳的任务应当被收尸。"""
        rc.save_runtime_config({"tasks": {"task_zombie_timeout_s": "120"}})
        now = int(time.time() * 1000)
        await _make_task("t-stale", "running", now - 600_000)  # 10 分钟前

        reaped = await ingest_api.reap_zombie_tasks()

        assert reaped == 1, "超过配置阈值（120s）无心跳的任务必须被收尸"
        record = await repo.get_ingest_task("t-stale")
        assert record["status"] == "failed"
        assert "中断" in (record.get("error") or ""), "必须给出可读的失败原因"

    @pytest.mark.asyncio
    async def test_active_task_is_not_reaped(self, db_env):
        """反向契约：心跳持续刷新的长任务（跑了 1 小时）绝不能被误杀。"""
        rc.save_runtime_config({"tasks": {"task_zombie_timeout_s": "120"}})
        now = int(time.time() * 1000)
        await _make_task("t-busy", "running", now)  # 刚 touch 过心跳

        reaped = await ingest_api.reap_zombie_tasks()

        assert reaped == 0, "正在推进阶段的任务不得被收尸"
        record = await repo.get_ingest_task("t-busy")
        assert record["status"] == "running"


class TestPhaseHeartbeat:
    @pytest.mark.asyncio
    async def test_pipeline_touches_every_phase(self, db_env, monkeypatch):
        """分阶段心跳：uploading → transcribing → chunking → indexing 都要 touch。

        没有它，"跑了 40 分钟的长录音"与"卡死 40 分钟"在数据上无法区分——
        收尸只能二选一地误杀或漏杀。
        """
        from app.core import task_policy

        touched: list[str] = []

        async def _fake_locked(*, heartbeat=None, **kwargs):
            for phase in ("uploading", "transcribing", "chunking", "indexing"):
                if heartbeat is not None:
                    await heartbeat(phase)
                    touched.append(phase)
            return {"chunks_added": 2}

        monkeypatch.setattr(ingest_api, "_ingest_locked", _fake_locked)

        class _File:
            filename = "lecture.wav"

            async def read(self) -> bytes:
                return b"audio"

        out = await ingest_api.submit_ingest_task(
            request="unit-test", user=_user(), session_id="s-phase", media_type="audio",
            file=_File(), lecture_date="", text_content="", subject="数学",
            course_id="default", chapter="", idempotency_key="idem-phase",
        )
        deadline = time.time() + 5
        while time.time() < deadline:
            rec = await repo.get_ingest_task(out["task_id"])
            if rec and rec["status"] == "succeeded":
                break
            await asyncio.sleep(0.05)

        assert touched == ["uploading", "transcribing", "chunking", "indexing"], "每个阶段都必须 touch"
        assert task_policy.effective()["task_phase_timeout_s"] >= 60

    @pytest.mark.asyncio
    async def test_heartbeat_refreshes_updated_at(self, db_env, monkeypatch):
        """心跳的落点必须是 updated_at——收尸判据读的就是它。"""
        from app.core import task_policy

        now_ms = int(time.time() * 1000)
        await _make_task("t-touch", "running", now_ms - 60_000)

        hook = ingest_api._phase_hook("t-touch")
        await hook("transcribing")

        record = await repo.get_ingest_task("t-touch")
        assert record["updated_at"] > now_ms - 60_000, "心跳必须刷新 updated_at"

        reaped = await ingest_api.reap_zombie_tasks()
        assert reaped == 0, "刚 touch 过的任务不该被收尸"
        assert task_policy.effective() is not None


class TestElasticConcurrency:
    def test_hot_tune_changes_gate_capacity_without_restart(self, db_env):
        """并发槽位热调：改配置即刻生效，不需要重启进程。"""
        rc.save_runtime_config({"tasks": {"ingest_max_concurrency": "4"}})

        gate = ingest_api._ingest_gate()

        assert gate.capacity == 4, "容量必须跟随运行时配置，而不是进程启动时定型"

    def test_guardrail_degrades_when_memory_is_low(self, db_env, monkeypatch):
        """保护层：宿主机可用内存过低时，无论管理员配了多少，并发必须降到 1。"""
        from app.core import task_policy

        rc.save_runtime_config({"tasks": {"ingest_max_concurrency": "8"}})
        monkeypatch.setattr(task_policy, "available_memory_mb", lambda: 80.0)

        eff = task_policy.effective()

        assert eff["ingest_max_concurrency"] == 1, "内存告警时必须强制串行，避免 OOM"
        assert eff["guardrail_applied"] is True, "降级原因必须可观测，不能默默改数字"

    def test_guardrail_not_applied_when_memory_is_plenty(self, db_env, monkeypatch):
        from app.core import task_policy

        rc.save_runtime_config({"tasks": {"ingest_max_concurrency": "4"}})
        monkeypatch.setattr(task_policy, "available_memory_mb", lambda: 4096.0)

        eff = task_policy.effective()

        assert eff["ingest_max_concurrency"] == 4
        assert eff["guardrail_applied"] is False


class TestConfigClamping:
    def test_out_of_range_values_are_clamped(self, db_env):
        """面板可以传任何数字：越界值必须夹紧到安全区间，不能让 1C2G 被调到 8。"""
        from app.core import task_policy

        rc.save_runtime_config({"tasks": {"ingest_max_concurrency": "99",
                                          "task_zombie_timeout_s": "1"}})

        eff = task_policy.effective()

        assert eff["ingest_max_concurrency"] == 8, "并发上限 8"
        assert eff["task_zombie_timeout_s"] == 120, "收尸阈值下限 120s（再短必误杀）"
