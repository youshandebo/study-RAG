# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""FastAPI 应用入口：统一挂载 v1 路由与静态资源。"""
from __future__ import annotations

import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app.api.v1 import (
    admin, admin_ops, auth, chat_stream, compare, course, evidence, exam,
    feedback, ingest, notebook, sessions,
)
from app.core.boot_probe import enforce_runtime_safety
from app.core.config import get_settings
from app.db.minio_client import BOARDS_DIR, ensure_static_dirs

settings = get_settings()

# 启动探针必须跑在**任何组件被使用之前**：多副本 + 无 Redis 时直接拒绝启动，
# 把"配额被放大 N 倍 / 闸门失效 / 限流互不可见"这类静默故障挡在上线之前。
# 它只读环境变量与启动参数，不做网络 IO，放在导入期是安全的。
RUNTIME_SAFETY = enforce_runtime_safety()

app = FastAPI(title=settings.app_name, version="1.0.0")


@app.middleware("http")
async def license_fingerprint_headers(request, call_next):
    """全局版权指纹响应头：所有 API 与 SSE 响应均携带许可声明，供审计/溯源。"""
    response = await call_next(request)
    response.headers["X-Powered-By"] = "AI-Classroom-Tutor"
    response.headers["X-License-Type"] = "AGPL-3.0-or-Commercial"
    response.headers["X-Commercial-License-Contact"] = "fennengxiong@qq.com"
    return response


@app.middleware("http")
async def golden_signals(request: Request, call_next):
    """黄金信号采集：流量、错误、延迟（饱和度与上游延迟在各自模块埋点）。

    两个必须守住的约束：

    1. **端点标签取路由模板，不取原始路径**。用 `request.url.path` 会让
       `/ingest/tasks/{task_id}` 这类路径为**每个任务**生成一条时间序列，
       基数随用户量线性增长——这是 Prometheus 客户端把 1C2G 拖垮的典型方式。
       匹配不到路由时回落 "unhandled"，而不是把路径塞进标签。
    2. **监控不得改变业务语义**：异常一律向上抛，只记录状态码后原样返回。
    """
    from app.core import metrics

    route = request.scope.get("route")
    endpoint = getattr(route, "path", None) or "unhandled"
    started = time.perf_counter()
    try:
        response = await call_next(request)
        status = str(response.status_code)
    except Exception:
        duration = time.perf_counter() - started
        metrics.inc("http_requests_total",
                    {"method": request.method, "endpoint": endpoint, "status": "500"})
        metrics.observe("http_request_duration_seconds", duration, {"endpoint": endpoint})
        raise
    duration = time.perf_counter() - started
    metrics.inc("http_requests_total",
                {"method": request.method, "endpoint": endpoint, "status": status})
    metrics.observe("http_request_duration_seconds", duration, {"endpoint": endpoint})
    return response


@app.get("/metrics", include_in_schema=False)
async def metrics_endpoint():
    """Prometheus Pull 端点：只在被拉取时渲染一次文本，无后台采集、无常驻开销。

    公开可读（不含密钥/用户数据），由网关侧按需限制来源 IP 即可。
    """
    from app.core import metrics
    from app.api.v1 import ingest

    # 收尸巡检搭在拉取动作上：Prometheus 的拉取节奏（15~60s）天然就是巡检
    # 频率，不需要常驻线程——1C2G 没有这个预算，而"心跳停摆 10 分钟才算死"
    # 对分钟级巡检来说绰绰有余。监控在，僵尸就被收。
    #
    # 失败必须收敛在端点内部：收尸是旁路动作，它一旦把 /metrics 打成 5xx，
    # 整套黄金指标会连同收尸本身一起失明。
    try:
        await ingest.reap_zombie_tasks()
    except Exception:  # pragma: no cover - 收尸失败不得影响可观测性
        pass

    # 饱和度：入库积压。放这里实时算而不是后台定时采样——省一个常驻任务，
    # 且 /metrics 的拉取频率（通常 15~60s）本身就够用。
    metrics.set_gauge("ingest_tasks_active", float(await ingest.active_task_count()))
    return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4; charset=utf-8")


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 文字笔记走 multipart 表单字段，放宽默认 1MB 上限（text 入库另有 1MB 硬限）
from starlette.formparsers import MultiPartParser

MultiPartParser.max_field_size = 8 * 1024 * 1024

ensure_static_dirs()
app.mount("/static", StaticFiles(directory=str(BOARDS_DIR.parent)), name="static")

prefix = settings.api_prefix
app.include_router(sessions.router, prefix=prefix, tags=["sessions"])
app.include_router(chat_stream.router, prefix=prefix, tags=["chat"])
app.include_router(compare.router, prefix=prefix, tags=["compare"])
app.include_router(ingest.router, prefix=prefix, tags=["ingest"])
app.include_router(evidence.router, prefix=prefix, tags=["evidence"])
app.include_router(admin.router, prefix=prefix, tags=["admin"])
app.include_router(exam.router, prefix=prefix, tags=["exam"])
app.include_router(course.router, prefix=prefix, tags=["course"])
app.include_router(auth.router, prefix=prefix, tags=["auth"])
# P1-C 运营后台：平台侧（租户/充值/FinOps/全量 bad-case）与租户侧（本租户导出）
app.include_router(admin_ops.router, prefix=prefix, tags=["ops"])
app.include_router(feedback.router, prefix=prefix, tags=["ops"])
# P2-B 错题本与知识补救：到期复习 / 复习结算 / 变式衍生 / 手动收录
app.include_router(notebook.router, prefix=prefix, tags=["notebook"])


@app.get("/api/v1/health")
async def health():
    return {
        "status": "ok",
        "mock_mode": settings.mock_mode,
        # 共享状态后端必须可观测：运维要能一眼看出"现在跑的是 Redis 还是进程内",
        # 否则多副本部署是否真的共享了配额，只能靠猜。
        "state": {
            "backend": RUNTIME_SAFETY["state_backend"],
            "environment": RUNTIME_SAFETY["environment"],
            "replicas": RUNTIME_SAFETY["replicas"],
            "acknowledged_process_local": RUNTIME_SAFETY["acknowledged"],
        },
        "real_models": [
            k for k, v in {
                "openai": settings.openai_api_key,
                "anthropic": settings.anthropic_api_key,
                "dashscope": settings.dashscope_api_key,
                "deepseek": settings.deepseek_api_key,
            }.items() if v
        ],
    }
