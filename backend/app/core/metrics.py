# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""Prometheus 文本格式的黄金指标（Golden Signals）暴露层。

为什么自己写而不用 prometheus_client
------------------------------------
本项目的开箱底线是 **1C2G**，且要求"零额外常驻内存"。官方客户端自带
`REGISTRY`、进程/GC 采集器与启动时的平台探测，对小型部署是纯粹的负担。
这里只做两件事：请求来了加个计数、被拉取时渲染一次文本。

设计约束（每一条都对应一个真实的 1C2G 事故模式）
------------------------------------------------
1. **没有后台任务、没有线程、没有定时采样**。指标是进程内计数器，只有
   /metrics 被拉取时才做一次 O(指标数) 的文本渲染。
2. **标签基数受控**。HTTP 端点标签取**路由模板**（`/ingest/tasks/{task_id}`）
   而不是请求路径——否则每个任务 id 都会产生一条新时间序列，内存随时间线性
   增长，这正是 Prometheus 客户端最常见的 OOM 来源。
3. **计数用普通 dict + 锁**，不做异步、不做批量。单次请求只多一次字典写与
   一次时间读取，开销在噪声级。
4. **直方图用固定桶**：不动态分桶、不保存原始样本，内存与请求数无关。

指标清单（黄金四信号 + 本项目特有的两条）
----------------------------------------
- `http_requests_total{method,endpoint,status}`        流量
- `http_request_duration_seconds{endpoint}` 直方图      延迟
- `ingest_tasks_active`                                饱和度（入库积压）
- `llm_provider_latency_seconds{provider,model,kind}`  上游依赖延迟
- `circuit_breaker_tripped_total{service}`             错误/熔断
"""
from __future__ import annotations

import threading
import time

_lock = threading.Lock()

# 直方图桶（秒）。覆盖"本地缓存命中"到"冷启动 + 上游慢"的完整区间。
# 桶是固定常量：动态分桶会随样本无限膨胀，是内存事故的经典来源。
DURATION_BUCKETS: tuple[float, ...] = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)

# ---- 计数型指标：{指标名: {(标签元组): 数值}} ----
_counters: dict[str, dict[tuple[tuple[str, str], ...], float]] = {}

# ---- 直方图：{指标名: {(标签元组): {"sum":, "count":, "buckets": {le: n}}}} ----
_histograms: dict[str, dict[tuple[tuple[str, str], ...], dict]] = {}

# ---- 仪表（直接存数值，赋值即生效）----
_gauges: dict[str, dict[tuple[tuple[str, str], ...], float]] = {}

# 单次渲染允许输出的最大样本行数。防止任何意外的标签泄漏把响应撑到几十 MB，
# 那样拉取方（Prometheus）会比被监控方先出问题。
_MAX_SERIES = 2000

# ---- 契约声明：这些指标**恒出现在输出里**（无样本时以 0 值暴露）----
# 为什么必须有：面板与告警规则常按 `up{job}` 之外还断言"某指标是否存在"。
# 若指标只在发生过一次 LLM 调用后才出现，那么"LLM 从未被调用"和"埋点坏了"
# 在 /metrics 上完全同形——这正是可观测性最怕的静默失效。
DECLARED_COUNTERS: tuple[str, ...] = ("http_requests_total", "circuit_breaker_tripped_total")
DECLARED_HISTOGRAMS: tuple[str, ...] = ("http_request_duration_seconds", "llm_provider_latency_seconds")
DECLARED_GAUGES: tuple[str, ...] = ("ingest_tasks_active",)

_METRIC_HELP = {
    "http_requests_total": "HTTP 请求总数（按方法/路由模板/状态码）",
    "http_request_duration_seconds": "HTTP 请求耗时（秒），直方图",
    "ingest_tasks_active": "当前处于运行中的入库任务数",
    "llm_provider_latency_seconds": "上游大模型/Embedding 调用延迟（秒）",
    "circuit_breaker_tripped_total": "断路器跳闸次数",
}


def _labels(pairs: dict[str, str] | None) -> tuple[tuple[str, str], ...]:
    if not pairs:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in pairs.items()))


def inc(name: str, labels: dict[str, str] | None = None, value: float = 1.0) -> None:
    """计数器自增。热路径上只做一次字典写 + 一次锁。"""
    key = _labels(labels)
    with _lock:
        bucket = _counters.setdefault(name, {})
        bucket[key] = bucket.get(key, 0.0) + value


def observe(name: str, seconds: float, labels: dict[str, str] | None = None) -> None:
    """直方图观测：累加 sum/count，并把样本计入所有 >= 该值的桶。"""
    key = _labels(labels)
    with _lock:
        series = _histograms.setdefault(name, {}).get(key)
        if series is None:
            series = {"sum": 0.0, "count": 0, "buckets": {le: 0 for le in DURATION_BUCKETS}}
            _histograms[name][key] = series
        series["sum"] += seconds
        series["count"] += 1
        for le in DURATION_BUCKETS:
            if seconds <= le:
                series["buckets"][le] += 1


def set_gauge(name: str, value: float, labels: dict[str, str] | None = None) -> None:
    key = _labels(labels)
    with _lock:
        _gauges.setdefault(name, {})[key] = float(value)


def reset() -> None:
    """清空全部指标（测试用）。生产路径不需要——进程退出即随内存释放。"""
    with _lock:
        _counters.clear()
        _histograms.clear()
        _gauges.clear()


def _fmt_labels(key: tuple[tuple[str, str], ...]) -> str:
    if not key:
        return ""
    inner = ",".join(f'{k}="{_escape(v)}"' for k, v in key)
    return "{" + inner + "}"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render() -> str:
    """渲染 Prometheus 文本格式（0.0.4  exposition format 的最小可用子集）。

    只在 /metrics 被拉取时调用：O(序列数)，没有任何预聚合缓存需要维护。
    """
    lines: list[str] = []
    emitted = 0

    with _lock:
        counter_snapshot = {n: dict(s) for n, s in _counters.items()}
        hist_snapshot = {n: {k: dict(v, buckets=dict(v["buckets"])) for k, v in s.items()}
                         for n, s in _histograms.items()}
        gauge_snapshot = {n: dict(s) for n, s in _gauges.items()}

    # 契约补全：声明过但还没有样本的指标，以 0 值暴露（不污染真实计数）。
    for name in DECLARED_COUNTERS:
        counter_snapshot.setdefault(name, {}).setdefault((), 0.0)
    for name in DECLARED_HISTOGRAMS:
        hist_snapshot.setdefault(name, {}).setdefault(
            (), {"sum": 0.0, "count": 0, "buckets": {le: 0 for le in DURATION_BUCKETS}}
        )
    for name in DECLARED_GAUGES:
        gauge_snapshot.setdefault(name, {}).setdefault((), 0.0)

    for name, series in sorted(counter_snapshot.items()):
        lines.append(f"# HELP {name} {_METRIC_HELP.get(name, name)}")
        lines.append(f"# TYPE {name} counter")
        for key, value in sorted(series.items()):
            if emitted >= _MAX_SERIES:
                break
            lines.append(f"{name}{_fmt_labels(key)} {_fmt_num(value)}")
            emitted += 1

    for name, series in sorted(hist_snapshot.items()):
        lines.append(f"# HELP {name} {_METRIC_HELP.get(name, name)}")
        lines.append(f"# TYPE {name} histogram")
        for key, data in sorted(series.items()):
            base = _fmt_labels(key)
            for le in DURATION_BUCKETS:
                if emitted >= _MAX_SERIES:
                    break
                lbl = _fmt_labels(key + (("le", _fmt_num(le)),))
                lines.append(f"{name}_bucket{lbl} {data['buckets'].get(le, 0)}")
                emitted += 1
            lines.append(f"{name}_sum{base} {_fmt_num(data['sum'])}")
            lines.append(f"{name}_count{base} {data['count']}")

    for name, series in sorted(gauge_snapshot.items()):
        lines.append(f"# HELP {name} {_METRIC_HELP.get(name, name)}")
        lines.append(f"# TYPE {name} gauge")
        for key, value in sorted(series.items()):
            if emitted >= _MAX_SERIES:
                break
            lines.append(f"{name}{_fmt_labels(key)} {_fmt_num(value)}")
            emitted += 1

    return "\n".join(lines) + "\n"


def _fmt_num(value: float) -> str:
    """整数不带小数点，浮点保留 6 位——Prometheus 能解析的最简形式。"""
    if value == int(value):
        return str(int(value))
    return f"{value:.6f}"


def timed(name: str, labels: dict[str, str] | None = None):
    """同步/异步通用的耗时观测装饰器：把函数耗时计入直方图。

    只记耗时、不改返回值、不吞异常——监控代码绝不能改变业务语义。
    """

    def _decorator(func):
        import functools

        if _is_coroutine(func):
            @functools.wraps(func)
            async def _async_wrapper(*args, **kwargs):
                started = time.perf_counter()
                try:
                    return await func(*args, **kwargs)
                finally:
                    observe(name, time.perf_counter() - started, labels)

            return _async_wrapper

        @functools.wraps(func)
        def _sync_wrapper(*args, **kwargs):
            started = time.perf_counter()
            try:
                return func(*args, **kwargs)
            finally:
                observe(name, time.perf_counter() - started, labels)

        return _sync_wrapper

    return _decorator


def _is_coroutine(func) -> bool:
    import inspect

    return inspect.iscoroutinefunction(func)
