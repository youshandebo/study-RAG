# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""关系存储仓库层：配置 POSTGRES_DSN 时走 SQLAlchemy 异步引擎（真实持久化）；
否则回落进程内仓储（演示/单机零依赖）。对上层 API 完全一致，调用方无感。

- 使用 Postgres 需安装驱动：pip install asyncpg "sqlalchemy[asyncio]>=2.0"
- 表结构首次连接自动 create_all；Postgres 故障自动熔断降级内存（记 ERROR 日志）
- tutor_state 属瞬态伴学状态，保持内存实现
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

_logger = logging.getLogger("app.db.relational")

_engine = None                 # AsyncEngine（惰性创建，create_async_engine 为同步调用）
_sessionmaker = None
_tables_ready = False
_pg_broken = False             # 初始化失败熔断：本进程内不再反复尝试

_memory_sessions: dict[str, dict[str, Any]] = {}
_memory_messages: dict[str, list[dict[str, Any]]] = {}
_memory_assets: dict[str, list[dict[str, Any]]] = {}


def _use_postgres() -> bool:
    """配置了 POSTGRES_DSN 且驱动/引擎可用时返回 True（本函数只建引擎，不建连）。"""
    global _engine, _sessionmaker, _pg_broken
    if _pg_broken:
        return False
    if _engine is not None:
        return True
    from app.core.config import get_settings

    dsn = get_settings().postgres_dsn
    if not dsn:
        return False
    try:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        _engine = create_async_engine(dsn, echo=False, pool_pre_ping=True)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
        _logger.info("Postgres 存储引擎已创建: %s", dsn.split("@")[-1])
        return True
    except Exception as exc:
        _pg_broken = True
        _logger.error("Postgres 引擎初始化失败（降级进程内存储）: %s", exc)
        return False


async def _ensure_tables() -> bool:
    """首次真正执行 SQL 前建表（幂等）；驱动缺失/连接失败熔断降级。"""
    global _tables_ready, _pg_broken
    if _tables_ready:
        return True
    try:
        from app.db.pg_models import Base

        assert _engine is not None
        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        _tables_ready = True
        _logger.info("Postgres 表结构就绪")
        return True
    except Exception as exc:
        _pg_broken = True
        _logger.error("Postgres 建表失败（降级进程内存储，重启进程前不再重试）: %s", exc)
        return False


async def _pg_session():
    assert _sessionmaker is not None
    return _sessionmaker()


# ---------------------------------------------------------------- sessions --
async def create_session(title: str = "新对话") -> dict[str, Any]:
    sid = uuid.uuid4().hex[:12]
    record = {
        "id": sid,
        "title": title or "新对话",
        "created_at": int(time.time() * 1000),
        "subject": None,
        "course_id": None,
        "chapter": None,
        "retrieval_mode": None,
        "time_alpha_override": None,
    }
    if _use_postgres() and await _ensure_tables():
        from app.db.pg_models import SessionRow

        async with await _pg_session() as s:
            s.add(SessionRow(**record))
            await s.commit()
        return {k: v for k, v in record.items() if v is not None or k in ("id", "title", "created_at")}
    _memory_sessions[sid] = record
    _memory_messages[sid] = []
    _memory_assets[sid] = []
    return {k: v for k, v in record.items() if k in ("id", "title", "created_at")}


async def list_sessions() -> list[dict[str, Any]]:
    if _use_postgres() and await _ensure_tables():
        from sqlalchemy import select

        from app.db.pg_models import SessionRow

        async with await _pg_session() as s:
            rows = (await s.execute(select(SessionRow).order_by(SessionRow.created_at.desc()))).scalars().all()
        return [
            {
                "id": r.id, "title": r.title, "created_at": r.created_at,
                "subject": r.subject, "course_id": r.course_id, "chapter": r.chapter,
                "retrieval_mode": r.retrieval_mode, "time_alpha_override": r.time_alpha_override,
            }
            for r in rows
        ]
    return sorted(_memory_sessions.values(), key=lambda r: r["created_at"], reverse=True)


async def rename_session(session_id: str, title: str) -> None:
    if _use_postgres() and await _ensure_tables():
        from sqlalchemy import update

        from app.db.pg_models import SessionRow

        async with await _pg_session() as s:
            await s.execute(update(SessionRow).where(SessionRow.id == session_id).values(title=title))
            await s.commit()
        return
    if session_id in _memory_sessions:
        _memory_sessions[session_id]["title"] = title


async def delete_session(session_id: str) -> None:
    if _use_postgres() and await _ensure_tables():
        from sqlalchemy import delete

        from app.db.pg_models import AssetRow, MessageRow, SessionRow

        async with await _pg_session() as s:
            for table in (MessageRow, AssetRow, SessionRow):
                await s.execute(delete(table).where(table.session_id == session_id))
            await s.commit()
        return
    _memory_sessions.pop(session_id, None)
    _memory_messages.pop(session_id, None)
    _memory_assets.pop(session_id, None)


async def update_session_meta(session_id: str, patch: dict[str, Any]) -> None:
    """课程绑定 / 检索模式等会话元数据（内存与 Postgres 双实现）。"""
    allowed = {"subject", "course_id", "chapter", "retrieval_mode", "time_alpha_override"}
    values = {k: v for k, v in patch.items() if k in allowed}
    if not values:
        return
    if _use_postgres() and await _ensure_tables():
        from sqlalchemy import update

        from app.db.pg_models import SessionRow

        async with await _pg_session() as s:
            await s.execute(update(SessionRow).where(SessionRow.id == session_id).values(**values))
            await s.commit()
        return
    if session_id in _memory_sessions:
        _memory_sessions[session_id].update(values)


# ---------------------------------------------------------------- messages --
async def append_message(session_id: str, message: dict[str, Any]) -> None:
    if _use_postgres() and await _ensure_tables():
        from app.db.pg_models import MessageRow

        row = MessageRow(
            session_id=session_id,
            created_at=int(message.get("createdAt") or time.time() * 1000),
            payload=json.dumps(message, ensure_ascii=False),
        )
        async with await _pg_session() as s:
            s.add(row)
            await s.commit()
        return
    _memory_messages.setdefault(session_id, []).append(message)


async def list_messages(session_id: str) -> list[dict[str, Any]]:
    if _use_postgres() and await _ensure_tables():
        from sqlalchemy import select

        from app.db.pg_models import MessageRow

        async with await _pg_session() as s:
            rows = (
                await s.execute(
                    select(MessageRow)
                    .where(MessageRow.session_id == session_id)
                    .order_by(MessageRow.created_at.asc(), MessageRow.id.asc())
                )
            ).scalars().all()
        return [json.loads(r.payload) for r in rows]
    return list(_memory_messages.get(session_id, []))


# ------------------------------------------------------------------ assets --
async def add_asset(session_id: str, asset: dict[str, Any]) -> dict[str, Any]:
    record = {"id": uuid.uuid4().hex[:12], "created_at": int(time.time() * 1000), **asset}
    if _use_postgres() and await _ensure_tables():
        from app.db.pg_models import AssetRow

        async with await _pg_session() as s:
            s.add(AssetRow(id=record["id"], session_id=session_id,
                           created_at=record["created_at"],
                           payload=json.dumps(record, ensure_ascii=False)))
            await s.commit()
        return record
    _memory_assets.setdefault(session_id, []).append(record)
    return record


async def list_assets(session_id: str) -> list[dict[str, Any]]:
    if _use_postgres() and await _ensure_tables():
        from sqlalchemy import select

        from app.db.pg_models import AssetRow

        async with await _pg_session() as s:
            rows = (
                await s.execute(
                    select(AssetRow)
                    .where(AssetRow.session_id == session_id)
                    .order_by(AssetRow.created_at.asc())
                )
            ).scalars().all()
        return [json.loads(r.payload) for r in rows]
    return list(_memory_assets.get(session_id, []))


# ------------------------------------------------------------- tutor state --
_tutor_state: dict[str, dict[str, Any]] = {}


async def get_tutor_state(session_id: str) -> dict[str, Any]:
    return _tutor_state.get(session_id, {"step_index": -1, "finished": False, "history": []})


async def save_tutor_state(session_id: str, state: dict[str, Any]) -> None:
    _tutor_state[session_id] = state
