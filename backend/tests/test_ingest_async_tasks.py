# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""入库任务化：长耗时流水线不该挂在 HTTP 请求上（故障注入 + 幂等闭环）。

为什么这份测试存在（对应尽调第 1 项）
------------------------------------
当前 `POST /ingest` 是同步契约：HTTP 连接在整个流水线（上传 → 压缩 → ASR/VLM →
切片 → 向量化 → 落库）期间被独占。这条链路动辄几十秒到数分钟，由此产生三类
在生产上必然出现、且无法靠"调大超时"解决的问题：

1. **网关超时必然发生**：Nginx / CDN 默认 60s 断连。任务还在跑，但响应已经
   被判失败——用户看到 504，服务侧继续烧 CPU，结果无人接收。
2. **断连后无法找回结果**：没有任务句柄，客户端重连后既查不到进度也拿不到
   结果，只能重传文件；重传又触发一次完整流水线，配额被重复消耗。
3. **重传无幂等**：同一个文件的两次提交之间没有去重依据，最终双份切片。

因此本测试钉住三条契约（实现前全部为红灯）：
- 提交立即返回 `task_id`，不阻塞在流水线上
- 任务状态可查询，且走持久化存储
- 相同幂等键重复提交只产生一个任务、一次入库
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


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    """每用例一套独立 SQLite 库，避免任务表在用例间互相污染。"""
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "async_tasks.db"))
    saved = (repo._engine, repo._sessionmaker, repo._tables_ready, repo._pg_broken)
    repo._engine, repo._sessionmaker, repo._tables_ready, repo._pg_broken = None, None, False, False
    yield
    # 兜底：任何残留后台任务都在这里取消。带着周期存活心跳活过用例边界会在
    # 后续用例里造成跨循环连接争用（详见 _drain_background_tasks 的说明），
    # 这条兜底保证即使某个用例忘了收尾，污染也不会溢出本文件。
    for _task in list(ingest_api._BACKGROUND_TASKS):
        _task.cancel()
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


class _SlowFile:
    """上传文件替身：提交端会 `await file.read()`，这里只给最小可用契约。"""

    filename = "lecture.wav"

    async def read(self) -> bytes:
        return b"fake-audio-bytes"


def _patch_slow_pipeline(monkeypatch, result: dict | None = None):
    """把真实流水线换成可控慢实现：用来观察请求是否被流水线同步拖住。"""
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow(*, heartbeat=None, **kwargs):
        started.set()
        await release.wait()
        return result or {"asset": {"id": "asset-async"}, "chunks_added": 3}

    monkeypatch.setattr(ingest_api, "_ingest_locked", _slow)
    return started, release


async def _drain_background_tasks(timeout_s: float = 5.0) -> None:
    """排空入库后台任务：残留任务会带着周期存活心跳越过用例边界活下去。

    为什么必须显式排空（单据1 之后的新约束）：贯穿式心跳让"仍在运行的后台
    任务"从"静默"变成"每 min(task_phase_timeout_s/3, 30s) 周期性写库"。本文件
    第一条用例刻意不放行流水线（它只验证"提交不阻塞"），于是那个后台任务会
    永久存活并持续 touch 数据库——它越过了用例与事件循环的边界，在后续用例
    （尤其是走 TestClient 独立事件循环的 /metrics 抓取）里造成跨循环的连接
    争用，表现为全量套件在 54% 附近的**间歇性挂起**：单跑本文件不复现，
    只在全量上下文触发，因此极易被误判成"套件随机挂死"或"工具限流"。

    排空而不是任其自然结束：残留任务的生命周期不受本用例控制，唯一确定的
    收尾是显式取消 + 等待。
    """
    tasks = list(ingest_api._BACKGROUND_TASKS)
    for task in tasks:
        task.cancel()
    if not tasks:
        return
    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), timeout=timeout_s)
    except Exception:
        pass


class TestSubmitReturnsImmediately:
    @pytest.mark.asyncio
    async def test_submit_does_not_block_on_pipeline(self, db_env, monkeypatch):
        """核心红灯：提交必须立刻回来，不能陪流水线一起耗到网关超时。"""
        _started, release = _patch_slow_pipeline(monkeypatch, result={"chunks_added": 3})

        try:
            out = await ingest_api.submit_ingest_task(
                request="unit-test", user=_user(), session_id="s-async", media_type="audio",
                file=_SlowFile(), lecture_date="", text_content="", subject="数学",
                course_id="default", chapter="", idempotency_key="idem-1",
            )

            assert out["task_id"], "必须返回可查询的任务句柄"
            assert out["status"] in ("queued", "running"), "提交瞬间任务尚未完成"
        finally:
            # 契约只到"提交立即返回"，但残留的后台任务必须收尾：先放行让流水线
            # 自然跑完（贯穿心跳随之在它自己的 finally 里被取消），再兜底排空，
            # 避免它带着周期心跳活过用例边界污染后续用例。
            release.set()
            await _drain_background_tasks()

    @pytest.mark.asyncio
    async def test_result_is_queryable_after_completion(self, db_env, monkeypatch):
        """任务完成后必须能从持久化任务表里取出结果（而不是丢给断开的连接）。"""
        started, release = _patch_slow_pipeline(monkeypatch, result={"chunks_added": 7})

        out = await ingest_api.submit_ingest_task(
            request="unit-test", user=_user(), session_id="s-async", media_type="audio",
            file=_SlowFile(), lecture_date="", text_content="", subject="数学",
            course_id="default", chapter="", idempotency_key="idem-query",
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        release.set()

        deadline = asyncio.get_event_loop().time() + 5
        record = None
        while asyncio.get_event_loop().time() < deadline:
            record = await repo.get_ingest_task(out["task_id"])
            if record and record["status"] == "succeeded":
                break
            await asyncio.sleep(0.05)

        assert record is not None and record["status"] == "succeeded", "终态结果必须落库可查"
        assert record["result"]["chunks_added"] == 7


class TestIdempotency:
    @pytest.mark.asyncio
    async def test_duplicate_submission_ingests_once(self, db_env, monkeypatch):
        """断连重传是常态：同一幂等键重复提交只能入库一次，不能双份切片。"""
        calls = {"n": 0}
        started = asyncio.Event()

        async def _counting(*, heartbeat=None, **kwargs):
            calls["n"] += 1
            started.set()
            return {"chunks_added": 1}

        monkeypatch.setattr(ingest_api, "_ingest_locked", _counting)

        kwargs = dict(
            request="unit-test", user=_user(), session_id="s-idem", media_type="audio",
            file=_SlowFile(), lecture_date="", text_content="", subject="数学",
            course_id="default", chapter="", idempotency_key="idem-dup",
        )
        first = await ingest_api.submit_ingest_task(**kwargs)
        second = await ingest_api.submit_ingest_task(**kwargs)
        await asyncio.wait_for(started.wait(), timeout=5)
        await asyncio.sleep(0.05)

        assert second["task_id"] == first["task_id"], "重复提交必须复用同一任务"
        assert calls["n"] == 1, f"同一幂等键只能执行一次流水线，实际执行 {calls['n']} 次"


class TestFailureVisibility:
    @pytest.mark.asyncio
    async def test_pipeline_failure_is_recorded_not_lost(self, db_env, monkeypatch):
        """后台任务没有调用栈可见性：失败原因必须写进任务记录，否则永远查不到。"""

        async def _boom(*, heartbeat=None, **kwargs):
            raise RuntimeError("ASR 服务不可用")

        monkeypatch.setattr(ingest_api, "_ingest_locked", _boom)

        out = await ingest_api.submit_ingest_task(
            request="unit-test", user=_user(), session_id="s-fail", media_type="audio",
            file=_SlowFile(), lecture_date="", text_content="", subject="数学",
            course_id="default", chapter="", idempotency_key="idem-fail",
        )

        deadline = asyncio.get_event_loop().time() + 5
        record = None
        while asyncio.get_event_loop().time() < deadline:
            record = await repo.get_ingest_task(out["task_id"])
            if record and record["status"] == "failed":
                break
            await asyncio.sleep(0.05)

        assert record is not None and record["status"] == "failed", "失败必须落到终态"
        assert "ASR 服务不可用" in (record.get("error") or ""), "失败原因必须可查证"
