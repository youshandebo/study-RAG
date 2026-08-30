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
_engine_kind = "memory"        # postgres | sqlite | memory

_memory_sessions: dict[str, dict[str, Any]] = {}
_memory_messages: dict[str, list[dict[str, Any]]] = {}
_memory_assets: dict[str, list[dict[str, Any]]] = {}
_memory_users: dict[str, dict[str, Any]] = {}


def _use_postgres() -> bool:
    """配置了 POSTGRES_DSN 且驱动可用时返回 True（本函数只建引擎，不建连）。"""
    global _engine, _sessionmaker, _pg_broken, _engine_kind
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
        _engine_kind = "postgres"
        _logger.info("Postgres 存储引擎已创建: %s", dsn.split("@")[-1])
        return True
    except Exception as exc:
        _pg_broken = True
        _logger.error("Postgres 引擎初始化失败（降级进程内存储）: %s", exc)
        return False


def _db_ready() -> bool:
    """统一入口：Postgres（配置 DSN）或 SQLite（默认，单容器持久化）。

    SQLite 库文件默认 backend/data/app.db（APP_DB_PATH 可覆盖），重启不丢数据，
    彻底解决"内存存储重启即失"。两者都不可用时才回落内存。
    """
    global _engine, _sessionmaker, _pg_broken, _engine_kind
    if _pg_broken or _engine is not None:
        return _engine is not None
    try:
        import os as _os

        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        db_path = _os.getenv("APP_DB_PATH", "").strip() or _os.path.join(
            _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))),
            "data", "app.db",
        )
        _os.makedirs(_os.path.dirname(db_path), exist_ok=True)
        _engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
        _engine_kind = "sqlite"
        _logger.info("SQLite 存储引擎已创建: %s", db_path)
        return True
    except Exception as exc:
        _pg_broken = True
        _logger.error("SQLite 引擎初始化失败（降级进程内存储）: %s", exc)
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
async def create_session(title: str = "新对话", owner: str | None = None) -> dict[str, Any]:
    sid = uuid.uuid4().hex[:12]
    record = {
        "id": sid,
        "title": title or "新对话",
        "created_at": int(time.time() * 1000),
        "owner": owner,
        "subject": None,
        "course_id": None,
        "chapter": None,
        "retrieval_mode": None,
        "time_alpha_override": None,
    }
    if _db_ready() and await _ensure_tables():
        from app.db.pg_models import SessionRow

        async with await _pg_session() as s:
            s.add(SessionRow(**record))
            await s.commit()
        return {k: v for k, v in record.items() if v is not None or k in ("id", "title", "created_at")}
    record["owner"] = owner
    _memory_sessions[sid] = record
    _memory_messages[sid] = []
    _memory_assets[sid] = []
    return {k: v for k, v in record.items() if k in ("id", "title", "created_at", "owner")}


async def list_sessions(owner: str | None = None, require_owner: bool = False) -> list[dict[str, Any]]:
    if _db_ready() and await _ensure_tables():
        from sqlalchemy import select

        from app.db.pg_models import SessionRow

        stmt = select(SessionRow).order_by(SessionRow.created_at.desc())
        if require_owner and owner:
            stmt = stmt.where(SessionRow.owner == owner)
        async with await _pg_session() as s:
            rows = (await s.execute(stmt)).scalars().all()
        return [
            {
                "id": r.id, "title": r.title, "created_at": r.created_at,
                "subject": r.subject, "course_id": r.course_id, "chapter": r.chapter,
                "retrieval_mode": r.retrieval_mode, "time_alpha_override": r.time_alpha_override,
                "owner": r.owner,
            }
            for r in rows
        ]
    result = sorted(_memory_sessions.values(), key=lambda r: r["created_at"], reverse=True)
    if require_owner and owner:
        result = [r for r in result if r.get("owner") == owner]
    return result


async def rename_session(session_id: str, title: str) -> None:
    if _db_ready() and await _ensure_tables():
        from sqlalchemy import update

        from app.db.pg_models import SessionRow

        async with await _pg_session() as s:
            await s.execute(update(SessionRow).where(SessionRow.id == session_id).values(title=title))
            await s.commit()
        return
    if session_id in _memory_sessions:
        _memory_sessions[session_id]["title"] = title


async def delete_session(session_id: str) -> None:
    if _db_ready() and await _ensure_tables():
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


async def get_session_owner(session_id: str) -> str | None:
    if _db_ready() and await _ensure_tables():
        from sqlalchemy import select

        from app.db.pg_models import SessionRow

        async with await _pg_session() as s:
            row = (await s.execute(select(SessionRow.owner).where(SessionRow.id == session_id))).scalar_one_or_none()
        return row
    return (_memory_sessions.get(session_id) or {}).get("owner")


async def update_session_meta(session_id: str, patch: dict[str, Any]) -> None:
    """课程绑定 / 检索模式等会话元数据（内存与 Postgres 双实现）。"""
    allowed = {"subject", "course_id", "chapter", "retrieval_mode", "time_alpha_override"}
    values = {k: v for k, v in patch.items() if k in allowed}
    if not values:
        return
    if _db_ready() and await _ensure_tables():
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
    if _db_ready() and await _ensure_tables():
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
    if _db_ready() and await _ensure_tables():
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
    if _db_ready() and await _ensure_tables():
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
    if _db_ready() and await _ensure_tables():
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


async def count_assets() -> int:
    """素材总数（管理后台统计；PG/内存双实现）。"""
    if _db_ready() and await _ensure_tables():
        from sqlalchemy import func, select

        from app.db.pg_models import AssetRow

        async with await _pg_session() as s:
            total = (await s.execute(select(func.count()).select_from(AssetRow))).scalar()
        return int(total or 0)
    return sum(len(v) for v in _memory_assets.values())


async def list_all_assets() -> list[dict[str, Any]]:
    """全量资产元数据（音频切片反查用；量级为课堂素材，千级以内可接受）。"""
    if _db_ready() and await _ensure_tables():
        from sqlalchemy import select

        from app.db.pg_models import AssetRow

        async with await _pg_session() as s:
            rows = s.execute(select(AssetRow)).scalars().all()
        return [json.loads(r.payload) for r in rows]
    return [a for v in _memory_assets.values() for a in v]


# ------------------------------------------------------------------ users --
async def create_user(email: str, password_hash: str, tier: str = "free") -> dict[str, Any]:
    user = {
        "id": uuid.uuid4().hex[:12],
        "email": email.lower(),
        "password_hash": password_hash,
        "tier": tier,
        "created_at": int(time.time() * 1000),
    }
    if _db_ready() and await _ensure_tables():
        from app.db.pg_models import UserRow

        async with await _pg_session() as s:
            s.add(UserRow(**user))
            await s.commit()
        return {k: v for k, v in user.items() if k != "password_hash"}
    _memory_users[user["id"]] = user
    return {k: v for k, v in user.items() if k != "password_hash"}


async def get_user_by_email(email: str) -> dict[str, Any] | None:
    if _db_ready() and await _ensure_tables():
        from sqlalchemy import select

        from app.db.pg_models import UserRow

        async with await _pg_session() as s:
            row = (await s.execute(select(UserRow).where(UserRow.email == email.lower()))).scalar_one_or_none()
        if row is None:
            return None
        return {"id": row.id, "email": row.email, "password_hash": row.password_hash,
                "tier": row.tier, "created_at": row.created_at}
    for u in _memory_users.values():
        if u["email"] == email.lower():
            return u
    return None


async def get_user_by_id(user_id: str) -> dict[str, Any] | None:
    if _db_ready() and await _ensure_tables():
        from sqlalchemy import select

        from app.db.pg_models import UserRow

        async with await _pg_session() as s:
            row = s.get(UserRow, user_id)
        if row is None:
            return None
        return {"id": row.id, "email": row.email, "tier": row.tier, "created_at": row.created_at}
    u = _memory_users.get(user_id)
    return {k: v for k, v in u.items() if k != "password_hash"} if u else None


async def count_users() -> int:
    if _db_ready() and await _ensure_tables():
        from sqlalchemy import func, select

        from app.db.pg_models import UserRow

        async with await _pg_session() as s:
            total = (await s.execute(select(func.count()).select_from(UserRow))).scalar()
        return int(total or 0)
    return len(_memory_users)


async def list_users() -> list[dict[str, Any]]:
    if _db_ready() and await _ensure_tables():
        from sqlalchemy import select

        from app.db.pg_models import UserRow

        async with await _pg_session() as s:
            rows = (await s.execute(select(UserRow).order_by(UserRow.created_at.desc()))).scalars().all()
        return [{"id": r.id, "email": r.email, "tier": r.tier, "created_at": r.created_at} for r in rows]
    return [
        {k: v for k, v in u.items() if k != "password_hash"}
        for u in sorted(_memory_users.values(), key=lambda x: x["created_at"], reverse=True)
    ]


async def set_user_tier(user_id: str, tier: str) -> bool:
    if _db_ready() and await _ensure_tables():
        from sqlalchemy import update

        from app.db.pg_models import UserRow

        async with await _pg_session() as s:
            result = await s.execute(update(UserRow).where(UserRow.id == user_id).values(tier=tier))
            await s.commit()
        return bool(result.rowcount)
    if user_id in _memory_users:
        _memory_users[user_id]["tier"] = tier
        return True
    return False


async def storage_used_bytes(owner: str) -> int:
    """用户素材存储占用（AssetRow.size_bytes 求和）。"""
    if _db_ready() and await _ensure_tables():
        from sqlalchemy import func, select

        from app.db.pg_models import AssetRow

        async with await _pg_session() as s:
            total = (
                await s.execute(select(func.coalesce(func.sum(AssetRow.size_bytes), 0)).where(AssetRow.owner == owner))
            ).scalar()
        return int(total or 0)
    return sum(int(a.get("size_bytes") or 0) for v in _memory_assets.values() for a in v if a.get("owner") == owner)


# ------------------------------------------------------------- tutor state --
_tutor_state: dict[str, dict[str, Any]] = {}


async def get_tutor_state(session_id: str) -> dict[str, Any]:
    return _tutor_state.get(session_id, {"step_index": -1, "finished": False, "history": []})


async def save_tutor_state(session_id: str, state: dict[str, Any]) -> None:
    _tutor_state[session_id] = state
