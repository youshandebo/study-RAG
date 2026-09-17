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

    async def _slow(*, user, session_id, media_type, file, lecture_date, text_content,
                    subject, course_id, chapter, raw):
        started.set()
        await release.wait()
        return result or {"asset": {"id": "asset-async"}, "chunks_added": 3}

    monkeypatch.setattr(ingest_api, "_ingest_locked", _slow)
    return started, release


class TestSubmitReturnsImmediately:
    @pytest.mark.asyncio
    async def test_submit_does_not_block_on_pipeline(self, db_env, monkeypatch):
        """核心红灯：提交必须立刻回来，不能陪流水线一起耗到网关超时。"""
        _started, _release = _patch_slow_pipeline(monkeypatch, result={"chunks_added": 3})

        out = await ingest_api.submit_ingest_task(
            request="unit-test", user=_user(), session_id="s-async", media_type="audio",
            file=_SlowFile(), lecture_date="", text_content="", subject="数学",
            course_id="default", chapter="", idempotency_key="idem-1",
        )

        assert out["task_id"], "必须返回可查询的任务句柄"
        assert out["status"] in ("queued", "running"), "提交瞬间任务尚未完成"

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

        async def _counting(*, user, session_id, media_type, file, lecture_date, text_content,
                            subject, course_id, chapter, raw):
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

        async def _boom(*, user, session_id, media_type, file, lecture_date, text_content,
                        subject, course_id, chapter, raw):
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
