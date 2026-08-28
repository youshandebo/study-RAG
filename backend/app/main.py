# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""FastAPI 应用入口：统一挂载 v1 路由与静态资源。"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.v1 import admin, chat_stream, compare, evidence, ingest, sessions
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

ensure_static_dirs()
app.mount("/static", StaticFiles(directory=str(BOARDS_DIR.parent)), name="static")

prefix = settings.api_prefix
app.include_router(sessions.router, prefix=prefix, tags=["sessions"])
app.include_router(chat_stream.router, prefix=prefix, tags=["chat"])
app.include_router(compare.router, prefix=prefix, tags=["compare"])
app.include_router(ingest.router, prefix=prefix, tags=["ingest"])
app.include_router(evidence.router, prefix=prefix, tags=["evidence"])
app.include_router(admin.router, prefix=prefix, tags=["admin"])


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
