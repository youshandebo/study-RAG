# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""FastAPI 应用入口：统一挂载 v1 路由与静态资源。"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.v1 import (
    admin, admin_ops, auth, chat_stream, compare, course, evidence, exam,
    feedback, ingest, notebook, sessions,
)
from app.core.config import get_settings
from app.db.minio_client import BOARDS_DIR, ensure_static_dirs

settings = get_settings()

app = FastAPI(title=settings.app_name, version="1.0.0")


@app.middleware("http")
async def license_fingerprint_headers(request, call_next):
    """全局版权指纹响应头：所有 API 与 SSE 响应均携带许可声明，供审计/溯源。"""
    response = await call_next(request)
    response.headers["X-Powered-By"] = "AI-Classroom-Tutor"
    response.headers["X-License-Type"] = "AGPL-3.0-or-Commercial"
    response.headers["X-Commercial-License-Contact"] = "fennengxiong@qq.com"
    return response


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
        "real_models": [
            k for k, v in {
                "openai": settings.openai_api_key,
                "anthropic": settings.anthropic_api_key,
                "dashscope": settings.dashscope_api_key,
                "deepseek": settings.deepseek_api_key,
            }.items() if v
        ],
    }
