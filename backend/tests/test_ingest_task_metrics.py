# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""入库任务时长指标 + 收尸闭环 + SSE 常量回归（红灯先行）。

为什么这份测试存在
------------------
`task_zombie_timeout_s = P99 × 2.5` 这条校准路径成立的前提，是 /metrics 里
有**任务全生命周期时长**的直方图。当前只有 HTTP 请求耗时（任务化后仅剩毫秒级
提交开销）与瞬时 running 计数——既算不出任务耗时的 P99，也因为共享桶封顶 60s
而把 90~180s 的 ASR 任务全塞进 +Inf，分位数退化成常数。

钉住的三个真实缺陷（实现前全部为红灯）：
1. 没有 `ingest_task_duration_seconds` 直方图，也没有覆盖 120/600/3600s 的桶；
2. SSE 端点引用了 07e8cb7 重构中被删掉定义的 `_TASK_HEARTBEAT_TIMEOUT_MS`，
   任何一次进度流请求都会 NameError——前端进度条唯一入口是坏的；
3. `reap_zombie_tasks` 没有任何生产调用方：僵尸既不被收，也从直方图里消失
   （幸存者偏差），需要把它挂在 /metrics 拉取动作上并单独计数。

观测窗口必须与收尸窗口同形：收尸判的是"running 多久没刷新 updated_at"。该窗口
**包含** `_acquire_slot` 的排队等待，所以计时从置 running 起算；而排队期间由
`_queue_heartbeat_loop` 持续续命（见 tests/test_ingest_queue_heartbeat.py），健康
排队不会被判死——链路两侧都得钉住，否则要么误杀、要么漏收。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

import app.db.relational as repo
from app.api.v1 import ingest as ingest_api
from app.api.v1.auth import AuthUser
from app.core import metrics
from app.main import app


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    """每用例一套独立 SQLite 库 + 干净指标，避免任务表与计数器跨用例污染。"""
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "task_metrics.db"))
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
    """上传文件替身：提交端只需要 `await file.read()` 这一份契约。"""

    filename = "lecture.wav"

    async def read(self) -> bytes:
        return b"fake-audio-bytes"


async def _await_terminal(task_id: str, status: str, timeout_s: float = 5.0) -> dict | None:
    """轮询到期望终态为止：后台任务写库是异步的，不能赌事件循环的调度时机。"""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    record = None
    while loop.time() < deadline:
        record = await repo.get_ingest_task(task_id)
        if record and record["status"] == status:
            return record
        await asyncio.sleep(0.05)
    return record


def _hist_count(name: str, labels: dict[str, str]) -> float:
    """直接读进程内直方图的 count：绕开渲染层，断言更精确也更稳定。"""
    series = metrics._histograms.get(name, {}).get(metrics._labels(labels))
    return float(series["count"]) if series else 0.0


def _counter_value(name: str) -> float:
    return float(metrics._counters.get(name, {}).get(metrics._labels(None), 0.0))


async def _inject_task(task_id: str, *, status: str, stale_ms: int = 0) -> None:
    """绕过提交端点直接登记任务：给 SSE/收尸用例提供确定的初始状态。"""
    now = int(time.time() * 1000) - stale_ms
    await repo.create_ingest_task({
        "id": task_id, "session_id": "s-metrics", "owner": "u-1", "tenant_id": "tenant-1",
        "idempotency_key": f"idem-{task_id}", "media_type": "audio", "filename": "lecture.wav",
        "status": status, "created_at": now, "updated_at": now, "finished_at": 0,
        "error": None, "result_json": "",
    })


class TestTaskDurationHistogramDeclared:
    def test_metric_is_declared_with_buckets_covering_long_tasks(self):
        """契约：直方图必须声明，且专用桶要盖住收尸阈值的可调区间。

        为什么必须独立分桶：共享的 DURATION_BUCKETS 封顶 60s，而一小时课堂
        录音走第三方 ASR 也要 90~180s——任务会全部落进 +Inf 桶，P99 恒等于
        +Inf，`P99 × 2.5` 退化成毫无指导意义的常数。
        """
        assert "ingest_task_duration_seconds" in metrics.DECLARED_HISTOGRAMS
        buckets = getattr(metrics, "TASK_DURATION_BUCKETS", ())
        for guard in (120.0, 600.0, 3600.0):
            assert any(le >= guard for le in buckets), (
                f"任务时长桶必须盖住 {guard}s：否则长任务全挤进 +Inf，P99 无法计算"
            )

    def test_zombie_counter_is_declared(self):
        """契约：收尸计数器必须声明并恒出现在 /metrics 里——0 也是有效信号，
        "从未发生"与"埋点坏了"在这一个指标上必须可区分。"""
        assert "ingest_tasks_zombie_total" in metrics.DECLARED_COUNTERS


class TestDurationIsObservedForEveryOutcome:
    @pytest.mark.asyncio
    async def test_success_duration_is_observed(self, db_env, monkeypatch):
        async def _ok(*, heartbeat=None, **kwargs):
            return {"chunks_added": 3}

        monkeypatch.setattr(ingest_api, "_ingest_locked", _ok)

        out = await ingest_api.submit_ingest_task(
            request="unit-test", user=_user(), session_id="s-ok", media_type="audio",
            file=_SlowFile(), lecture_date="", text_content="", subject="数学",
            course_id="default", chapter="", idempotency_key="idem-ok",
        )
        assert await _await_terminal(out["task_id"], "succeeded") is not None, "成功任务必须落终态"

        count = _hist_count("ingest_task_duration_seconds",
                            {"outcome": "succeeded", "media_type": "audio"})
        assert count == 1.0, "成功任务的全程耗时必须进直方图，否则 P99 没有样本可算"

    @pytest.mark.asyncio
    async def test_failure_duration_is_observed(self, db_env, monkeypatch):
        """失败样本必须计入——只看成功样本会把 P99 系统性算小。

        收尸阈值要盖住的是"卡住但还活着"的长尾，而这类任务恰恰大多后来失败了；
        把它们排除在校准之外，等于按最好的情况设阈值，必然误杀。
        """
        async def _boom(*, heartbeat=None, **kwargs):
            raise RuntimeError("ASR 上游不可用")

        monkeypatch.setattr(ingest_api, "_ingest_locked", _boom)

        out = await ingest_api.submit_ingest_task(
            request="unit-test", user=_user(), session_id="s-boom", media_type="audio",
            file=_SlowFile(), lecture_date="", text_content="", subject="数学",
            course_id="default", chapter="", idempotency_key="idem-boom",
        )
        assert await _await_terminal(out["task_id"], "failed") is not None, "失败任务必须落终态"

        count = _hist_count("ingest_task_duration_seconds",
                            {"outcome": "failed", "media_type": "audio"})
        assert count == 1.0, "失败任务也必须计时：校准不能只看成功样本"

    @pytest.mark.asyncio
    async def test_queue_timeout_duration_is_observed(self, db_env, monkeypatch):
        """排队超时同样是一条终态出口，不得漏观测。

        僵尸窗口包含 `_acquire_slot` 的排队等待，这段等待在收尸眼里与执行时间
        同权——漏记它会低估 P99。（排队期间现已按 min(阶段超时/3, 30s) 续心跳，
        因此健康排队不会被判死，只由自己的 `GateTimeout` 结尾。）
        """
        async def _queue_full(timeout_s, heartbeat=None):
            raise ingest_api.GateTimeout("排队超时（测试注入）")

        monkeypatch.setattr(ingest_api, "_acquire_slot", _queue_full)

        out = await ingest_api.submit_ingest_task(
            request="unit-test", user=_user(), session_id="s-q", media_type="audio",
            file=_SlowFile(), lecture_date="", text_content="", subject="数学",
            course_id="default", chapter="", idempotency_key="idem-qt",
        )
        record = await _await_terminal(out["task_id"], "failed")
        assert record is not None and "排队超时" in (record.get("error") or "")

        count = _hist_count("ingest_task_duration_seconds",
                            {"outcome": "queue_timeout", "media_type": "audio"})
        assert count == 1.0, "排队超时必须与成功/失败同权计入时长分布"


class TestLabelCardinalityGuard:
    @pytest.mark.asyncio
    async def test_unknown_media_type_is_normalized_to_other(self, db_env, monkeypatch):
        """非法 media_type 必须归一成 "other"，不得原样进标签。

        media_type 是表单自由文本，直接当标签值时每个取值都会新建一条时间
        序列——这正是"指标基数爆炸把 1C2G 内存打爆"的标准入口；白名单归一
        之后 `_MAX_SERIES` 才有意义。
        """
        async def _ok(*, heartbeat=None, **kwargs):
            return {"chunks_added": 1}

        monkeypatch.setattr(ingest_api, "_ingest_locked", _ok)

        out = await ingest_api.submit_ingest_task(
            request="unit-test", user=_user(), session_id="s-label",
            media_type="application/x-weird-stream",
            file=_SlowFile(), lecture_date="", text_content="", subject="数学",
            course_id="default", chapter="", idempotency_key="idem-label",
        )
        assert await _await_terminal(out["task_id"], "succeeded") is not None

        assert _hist_count("ingest_task_duration_seconds",
                           {"outcome": "succeeded", "media_type": "other"}) == 1.0
        assert 'media_type="application/x-weird-stream"' not in metrics.render(), (
            "原始 media_type 绝不能成为标签值")


class TestSSEStreamRegression:
    @pytest.mark.asyncio
    async def test_progress_stream_emits_without_name_error(self, db_env):
        """回归钉：SSE 端点不得因缺失 `_TASK_HEARTBEAT_TIMEOUT_MS` 而崩溃。

        07e8cb7 重构删掉了该常量的定义，却保留了 `/ingest/tasks/{id}/events`
        里的引用——任何一次前端进度订阅都会在首帧前 NameError。这条用例把
        "删了定义却留着引用"这类重构事故钉死在 CI 上。
        """
        await _inject_task("task-sse", status="succeeded")

        resp = await ingest_api.stream_ingest_task("task-sse", _user())
        chunks = [chunk.decode() if isinstance(chunk, bytes) else chunk
                  async for chunk in resp.body_iterator]

        assert chunks, "SSE 至少要吐出一帧事件"
        assert "succeeded" in "".join(chunks), "已终态的任务应推出终态事件后收尾"


class TestReapHookOnMetricsPull:
    @pytest.mark.asyncio
    async def test_scrape_reaps_zombies_and_counts_them(self, db_env):
        """收尸必须挂在拉取动作上——这是唯一零常驻成本的触发器。

        为什么不用后台定时任务：1C2G 没有常驻线程预算，而 Prometheus 的拉取
        节奏（15~60s）本身就是天然的巡检频率；监控在，僵尸就被收。

        为什么还要单独计数：被收尸的任务没有正常回调，永远进不了时长直方图
        （幸存者偏差）。不把收尸数暴露出来，"误杀了健康慢任务"在线完全失明。
        """
        await _inject_task("task-zombie", status="running", stale_ms=1_800_000)

        with TestClient(app) as client:
            body = client.get("/metrics").text

        record = await repo.get_ingest_task("task-zombie")
        assert record is not None and record["status"] == "failed", (
            "僵尸任务必须在 /metrics 被拉取时收尸，否则永远停在 running")
        assert "进程中断" in (record.get("error") or "")

        assert _counter_value("ingest_tasks_zombie_total") == 1.0, (
            "收尸数必须计入计数器，纠正幸存者偏差")
        assert "ingest_tasks_zombie_total" in body

    @pytest.mark.asyncio
    async def test_healthy_running_task_survives_scrape(self, db_env):
        """反向契约：心跳还在推进的任务不得被顺手误杀。

        收尸一旦挂在全局端点上，杀伤面就是"每次拉取都扫一遍全表"——阈值或
        判据写错时，这条用例是第一时间亮起的红灯。
        """
        await _inject_task("task-alive", status="running")  # 心跳刚刷新

        with TestClient(app) as client:
            assert client.get("/metrics").status_code == 200

        record = await repo.get_ingest_task("task-alive")
        assert record is not None and record["status"] == "running", (
            "心跳活着的任务不得被收尸")
