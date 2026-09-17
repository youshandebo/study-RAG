# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""关系存储仓库层：配置 POSTGRES_DSN 时走 SQLAlchemy 异步引擎（真实持久化）；
否则回落进程内仓储（演示/单机零依赖）。对上层 API 完全一致，调用方无感。

- 使用 Postgres 需安装驱动：pip install asyncpg "sqlalchemy[asyncio]>=2.0"
- 表结构首次连接自动 create_all；Postgres 故障自动熔断降级内存（记 ERROR 日志）
- 苏格拉底 FSM 状态见 `services/agent/socratic_store.py`（不再复用本模块的
  `tutor_state` 进程内字典——阶段必须跨副本一致，放内存会导致负载均衡下漂移）
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

# 增量列迁移表：(表, 列, DDL 类型)。
# create_all 只建**缺失的表**，对已存在的表**不会加列**——存量部署升级后
# 直接用新字段会报 "no such column"。这里做幂等的 ALTER TABLE 补列，
# SQLite 与 Postgres 语法一致。
_SCHEMA_PATCHES: tuple[tuple[str, str, str], ...] = (
    ("users", "tenant_id", "VARCHAR(64)"),
    ("users", "role", "VARCHAR(20)"),
    # P2-B：存量 socratic_sessions 表补 pending_quiz_json（create_all 不会加列）
    ("socratic_sessions", "pending_quiz_json", "TEXT"),
)


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


async def _apply_schema_patches() -> None:
    """幂等补列：让存量库平滑获得新增字段。

    先探测列是否存在（SQLAlchemy inspector 对 SQLite/Postgres 通用），
    只在缺失时 ALTER——不用"捕获异常当成功"，否则权限/连接类真实错误
    会被静默吞掉，等到查询时才以更难懂的方式爆出来。
    """
    from sqlalchemy import inspect as sa_inspect, text

    assert _engine is not None
    async with _engine.begin() as conn:
        for table, column, ddl_type in _SCHEMA_PATCHES:
            def _missing(sync_conn, _t: str = table, _c: str = column) -> bool:
                try:
                    cols = {c["name"] for c in sa_inspect(sync_conn).get_columns(_t)}
                except Exception:
                    return False  # 表还不存在（create_all 已建，理论不会走到）
                return bool(cols) and _c not in cols

            if await conn.run_sync(_missing):
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))
                _logger.info("表结构补列: %s.%s %s", table, column, ddl_type)


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
        await _apply_schema_patches()
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


def db_ready() -> bool:
    """对外：存储引擎已就绪（驱动可用 + 驱动已建）。"""
    return _db_ready()


async def db_session():
    """对外：取一个会话，并**确保表结构已建**。

    运营台账等模块（services/ops）共用同一套引擎与建表逻辑，避免第二套
    连接方式导致"表在 A 路径建了、B 路径查不到"的漂移。
    """
    if not (_db_ready() and await _ensure_tables()):
        return None
    return await _pg_session()


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


# ------------------------------------------------------------ 入库任务 ----
# 任务化是长耗时流水线的唯一出路：状态必须落库而不是放进程内存——
# 内存 dict 在多副本下会让"提交在 A、轮询打到 B"直接查不到任务，
# 且进程重启会丢掉全部进行中的任务。
_memory_ingest_tasks: dict[str, dict[str, Any]] = {}

_TERMINAL_TASK_STATES = ("succeeded", "failed")


async def create_ingest_task(task: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """登记一个新任务。返回 (记录, 是否新建)。

    **唯一索引冲突 = 重传命中既有任务**，此时返回已有记录且 `created=False`
    ——调用方据此跳过再次启动流水线。判断交给数据库而不是"先查一遍"：
    并发重传时两个请求都会查不到、都去写，必然重复入库。
    """
    if _db_ready() and await _ensure_tables():
        from sqlalchemy.exc import IntegrityError

        from app.db.pg_models import IngestTaskRow

        row = IngestTaskRow(**task)
        async with await _pg_session() as s:
            s.add(row)
            try:
                await s.commit()
            except IntegrityError:
                await s.rollback()
                existing = await get_ingest_task_by_key(
                    task.get("tenant_id", ""), task.get("idempotency_key", "")
                )
                if existing is not None:
                    return existing, False
                raise
        return task, True

    key = (task.get("tenant_id", ""), task.get("idempotency_key", ""))
    for existing in _memory_ingest_tasks.values():
        if (existing.get("tenant_id", ""), existing.get("idempotency_key", "")) == key:
            return existing, False
    _memory_ingest_tasks[task["id"]] = dict(task)
    return task, True


async def get_ingest_task(task_id: str) -> dict[str, Any] | None:
    if _db_ready() and await _ensure_tables():
        from sqlalchemy import select

        from app.db.pg_models import IngestTaskRow

        async with await _pg_session() as s:
            row = (await s.execute(
                select(IngestTaskRow).where(IngestTaskRow.id == task_id)
            )).scalars().first()
        return _task_to_dict(row) if row is not None else None
    return dict(_memory_ingest_tasks.get(task_id)) if task_id in _memory_ingest_tasks else None


async def get_ingest_task_by_key(tenant_id: str, key: str) -> dict[str, Any] | None:
    """按幂等键查任务（唯一索引冲突后的回查、重连续传的命中复用）。"""
    if _db_ready() and await _ensure_tables():
        from sqlalchemy import select

        from app.db.pg_models import IngestTaskRow

        async with await _pg_session() as s:
            row = (await s.execute(
                select(IngestTaskRow).where(IngestTaskRow.tenant_id == tenant_id,
                                            IngestTaskRow.idempotency_key == key)
            )).scalars().first()
        return _task_to_dict(row) if row is not None else None
    for existing in _memory_ingest_tasks.values():
        if existing.get("tenant_id") == tenant_id and existing.get("idempotency_key") == key:
            return dict(existing)
    return None


async def update_ingest_task(task_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    """更新任务状态。**终态不可逆**：不允许把 succeeded/failed 改回或改掉。

    原因：迟到的重试（网络分区后两个副本都在跑同一个任务）可能把失败结果
    覆盖成成功，产生"幽灵成功"——用户看到成功但素材根本没入库。
    """
    patch = dict(patch)
    patch["updated_at"] = int(time.time() * 1000)

    if _db_ready() and await _ensure_tables():
        from sqlalchemy import select

        from app.db.pg_models import IngestTaskRow

        async with await _pg_session() as s:
            row = (await s.execute(
                select(IngestTaskRow).where(IngestTaskRow.id == task_id)
            )).scalars().first()
            if row is None:
                return None
            if row.status in _TERMINAL_TASK_STATES:
                return _task_to_dict(row)
            for field, value in patch.items():
                if hasattr(row, field):
                    setattr(row, field, value)
            await s.commit()
            await s.refresh(row)
            return _task_to_dict(row)

    record = _memory_ingest_tasks.get(task_id)
    if record is None:
        return None
    if record.get("status") in _TERMINAL_TASK_STATES:
        return dict(record)
    record.update(patch)
    return dict(record)


async def list_zombie_ingest_tasks(older_than_ms: int) -> list[dict[str, Any]]:
    """找出 `running` 但心跳已停摆的任务（持有者进程被杀 / OOM / 滚动发布）。

    这类任务永远不会自己变终态，客户端会一直轮询到超时——必须显式收尸，
    把它们标记成失败并给出可读原因。
    """
    cutoff = int(time.time() * 1000) - older_than_ms
    if _db_ready() and await _ensure_tables():
        from sqlalchemy import select

        from app.db.pg_models import IngestTaskRow

        async with await _pg_session() as s:
            rows = (await s.execute(
                select(IngestTaskRow).where(IngestTaskRow.status == "running",
                                            IngestTaskRow.updated_at < cutoff)
            )).scalars().all()
        return [_task_to_dict(r) for r in rows]
    return [dict(t) for t in _memory_ingest_tasks.values()
            if t.get("status") == "running" and (t.get("updated_at") or 0) < cutoff]


def _task_to_dict(row: Any) -> dict[str, Any]:
    result_raw = getattr(row, "result_json", "") or ""
    return {
        "id": row.id,
        "session_id": row.session_id,
        "owner": row.owner,
        "tenant_id": row.tenant_id,
        "idempotency_key": row.idempotency_key,
        "media_type": row.media_type,
        "filename": row.filename,
        "status": row.status,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "finished_at": row.finished_at,
        "error": row.error,
        "result": json.loads(result_raw) if result_raw else None,
    }


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
async def create_user(
    email: str, password_hash: str, tier: str = "free", tenant_id: str | None = None
) -> dict[str, Any]:
    user = {
        "id": uuid.uuid4().hex[:12],
        "email": email.lower(),
        "password_hash": password_hash,
        "tier": tier,
        "created_at": int(time.time() * 1000),
        "tenant_id": tenant_id or None,
        "role": "member",
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
                "tier": row.tier, "created_at": row.created_at,
                "tenant_id": getattr(row, "tenant_id", None)}
    for u in _memory_users.values():
        if u["email"] == email.lower():
            return u
    return None


async def get_user_by_id(user_id: str) -> dict[str, Any] | None:
    if _db_ready() and await _ensure_tables():
        from sqlalchemy import select

        from app.db.pg_models import UserRow

        async with await _pg_session() as s:
            row = await s.get(UserRow, user_id)  # AsyncSession.get 为协程，漏 await 会把协程当行对象用
        if row is None:
            return None
        return {"id": row.id, "email": row.email, "tier": row.tier,
                "created_at": row.created_at,
                "tenant_id": getattr(row, "tenant_id", None),
                "role": getattr(row, "role", None) or "member"}
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
        return [
            {"id": r.id, "email": r.email, "tier": r.tier, "created_at": r.created_at,
             "tenant_id": getattr(r, "tenant_id", None),
             "role": getattr(r, "role", None) or "member"}
            for r in rows
        ]
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


async def set_user_tenant(user_id: str, tenant_id: str | None) -> bool:
    """分配/变更用户的租户归属（运营后台操作）。

    这是**唯一**能为用户指定租户的入口，且只在服务端（管理接口）调用——
    用户自己无法通过任何请求参数修改自己的 tenant_id。
    """
    if _db_ready() and await _ensure_tables():
        from sqlalchemy import update

        from app.db.pg_models import UserRow

        async with await _pg_session() as s:
            result = await s.execute(
                update(UserRow).where(UserRow.id == user_id).values(tenant_id=tenant_id or None)
            )
            await s.commit()
        return bool(result.rowcount)
    if user_id in _memory_users:
        _memory_users[user_id]["tenant_id"] = tenant_id or None
        return True
    return False


async def set_user_role(user_id: str, role: str) -> bool:
    """设置用户角色（member / tenant_admin）。

    平台超管不走这张表——它用管理口令换 admin JWT（见 api/v1/admin.py）。
    此处只授予"能不能导出本租户 bad-case"这类租户内权限。
    """
    allowed = {"member", "tenant_admin"}
    role = role if role in allowed else "member"
    if _db_ready() and await _ensure_tables():
        from sqlalchemy import update

        from app.db.pg_models import UserRow

        async with await _pg_session() as s:
            result = await s.execute(update(UserRow).where(UserRow.id == user_id).values(role=role))
            await s.commit()
        return bool(result.rowcount)
    if user_id in _memory_users:
        _memory_users[user_id]["role"] = role
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
