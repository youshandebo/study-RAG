"""Celery 异步任务队列配置（可选依赖：无 Redis 时由 workers.tasks 内的同步兜底执行）。"""
from __future__ import annotations

from app.core.config import get_settings

settings = get_settings()

try:
    from celery import Celery

    celery_app = Celery(
        "classroom_ingest",
        broker=settings.redis_url or "redis://localhost:6379/0",
        backend=settings.redis_url or "redis://localhost:6379/0",
    )
    celery_app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        task_track_started=True,
        worker_max_tasks_per_child=20,
    )
except ImportError:  # pragma: no cover - celery 未安装时保持可导入
    celery_app = None  # type: ignore[assignment]
