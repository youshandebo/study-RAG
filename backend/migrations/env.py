# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""Alembic 运行环境：连接信息从项目统一配置解析，模型元数据取自 pg_models。

两个要点
--------
1. **连接信息单一来源**：不把 DSN 写进 alembic.ini。项目已经有 `.env` +
   runtime_config 两级配置，再在 alembic.ini 里维护第三份必然漂移。
   解析顺序：ALEMBIC_DATABASE_URL > POSTGRES_DSN > 本地 SQLite。
2. **async 引擎**：项目用的是 `sqlite+aiosqlite` / `postgresql+asyncpg`
   异步驱动，所以走 `async_engine_from_config` + `run_sync`，
   而不是 Alembic 默认的同步引擎。
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

_BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.db.pg_models import Base  # noqa: E402

config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    """解析数据库连接串：显式覆盖 > Postgres 配置 > 本地 SQLite。"""
    explicit = os.getenv("ALEMBIC_DATABASE_URL", "").strip()
    if explicit:
        return explicit
    from app.core.config import get_settings

    dsn = get_settings().postgres_dsn.strip()
    if dsn:
        return dsn
    # 与 relational._db_ready() 的默认路径保持一致：backend/data/app.db
    db_path = os.getenv("APP_DB_PATH", "").strip() or str(_BACKEND / "data" / "app.db")
    pathlib.Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{db_path}"


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL 文本，不连库（适合 review 与 DBA 审核）。"""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        # SQLite 不支持大多数 ALTER，batch 模式会重建表来模拟
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    config.set_main_option("sqlalchemy.url", _database_url())
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
