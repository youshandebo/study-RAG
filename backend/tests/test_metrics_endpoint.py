# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""/metrics 黄金指标：Pull 端点存在性 + 关键信号可观测（红灯先行）。

为什么必须补这块
----------------
系统目前只有 `audit_logs`（责任溯源："谁改的"），没有 APM（性能定界："为什么慢"）。
线上卡顿时，运维手里只有一条条操作日志，无法回答"是 HTTP 层排队、入库任务堆积、
还是上游 LLM 变慢"——这是秒级定界能力的缺失。

1C2G 约束下的实现口径
---------------------
**严禁**在本地拉起 Prometheus/Grafana，也**不许**引入常驻采集线程或后台任务：
指标全部是进程内计数器，只在 /metrics 被拉取时才做一次文本渲染（O(指标数)），
没有额外常驻内存与 CPU。直方图用固定桶，标签基数受控（端点用路由模板而非
原始路径，避免 `/ingest/tasks/{id}` 这类高基数标签把内存吃穿）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


REQUIRED_METRICS = (
    "http_requests_total",
    "http_request_duration_seconds",
    "ingest_tasks_active",
    "llm_provider_latency_seconds",
    "circuit_breaker_tripped_total",
)


class TestMetricsEndpointExists:
    def test_metrics_is_exposed_as_pull_endpoint(self, client):
        """最基础的红灯：现在压根没有 /metrics，线上无法被任何监控系统拉取。"""
        resp = client.get("/metrics")

        assert resp.status_code == 200, "/metrics 必须是可拉取的公开端点"
        assert "text/plain" in resp.headers.get("content-type", "")

    def test_all_golden_signals_are_present(self, client):
        body = client.get("/metrics").text

        for name in REQUIRED_METRICS:
            assert name in body, f"缺少黄金指标 {name}"

    def test_histogram_has_le_buckets_and_sum(self, client):
        """直方图必须带 le 桶与 _sum/_count，否则 rate/quantile 算不出来。"""
        body = client.get("/metrics").text

        # 注意标签顺序：endpoint 在前、le 在后，断言只校验 bucket 形态本身
        assert "http_request_duration_seconds_bucket{" in body
        assert 'le="' in body
        assert "http_request_duration_seconds_sum" in body
        assert "http_request_duration_seconds_count" in body


class TestMetricsActuallyCollect:
    def test_http_requests_are_counted(self, client):
        """计数器必须真的在动：请求一次，计数就该涨一次（不是写死的占位输出）。"""
        before = _metric_value(client, "http_requests_total")
        client.get("/api/v1/health")
        after = _metric_value(client, "http_requests_total")

        assert after > before, "HTTP 请求必须被中间件计数"

    def test_circuit_breaker_trips_are_counted(self, client):
        """熔断次数：断路器跳闸必须被记账，否则只能靠日志猜。"""
        from app.core.circuit import get_breaker, reset_all

        reset_all()
        breaker = get_breaker("metrics-test-upstream")
        threshold = breaker.config.failure_threshold
        for _ in range(threshold):
            breaker.record_failure("simulated upstream failure")

        body = client.get("/metrics").text

        assert 'circuit_breaker_tripped_total{service="metrics-test-upstream"}' in body
        reset_all()


def _metric_value(client, name: str) -> float:
    """从 /metrics 文本里累加同名指标的所有样本值（忽略标签差异）。"""
    total = 0.0
    for line in client.get("/metrics").text.splitlines():
        if line.startswith("#"):
            continue
        if line.split("{")[0] == name:
            try:
                total += float(line.rsplit(" ", 1)[-1])
            except ValueError:
                continue
    return total
