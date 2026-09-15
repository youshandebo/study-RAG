#!/usr/bin/env python
# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""容器启动期 schema 演进：把 alembic 迁移接到启动路径上（fail-fast）。

为什么需要它
------------
镜像里没有迁移入口时，新版容器挂上旧数据库**不会自动演进而只是照常启动**，
于是：
- 缺列/缺表要等到第一个请求打到那条路径才炸（而且多半是 500，不是明确的启动失败）；
- 运维必须记得手动进容器跑 `alembic upgrade head`——"记得"不是一种机制。

这个脚本让"启动即对齐 schema"成为默认行为：迁移失败就退出（容器进入重启循环，
问题在日志里一眼可见），而不是带着不匹配的 schema 对外服务。

一个必须处理的坑：存量库认领
----------------------------
本项目有**两条建表路径**：运行期的 `Base.metadata.create_all`（+ 幂等补列）
与 Alembic 迁移。老的部署是被 create_all 建起来的——库里**有表但没有
`alembic_version`**。此时直接 `upgrade head` 会从第一个 revision 开始跑，
第一句就是 `CREATE TABLE sessions` → `table already exists` 直接失败。

所以走两步：
1. **幂等建缺失的表**（`create_all` 只创建不存在的表，改不动已有表）；
2. **`alembic stamp head` 认领**：声明"当前结构即 head"，使后续增量迁移可正常应用。

只做第 2 步是错的——认领之后 `upgrade` 成了空操作，`audit_logs` /
`mistake_notebook` 这类较晚加入的表永远不会被创建，而脚本却会声称"已对齐 head"。

既有表**缺失的增量列**不在本脚本职责内：由运行期 `_SCHEMA_PATCHES` 补齐，
这是本项目"两条路径收敛"的既定分工（迁移负责表与索引，运行期补列兜底）。

开关
----
- `AUTO_MIGRATE=0`：完全跳过（留给"手工控库"的部署）
- 其余情况：判定 → 认领（如需要）→ upgrade head，任一步失败即非 0 退出
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _enabled() -> bool:
    return (os.getenv("AUTO_MIGRATE", "1") or "").strip().lower() not in ("0", "false", "no", "off")


def _database_url() -> str:
    """与 migrations/env.py 完全同一优先级，避免"迁移跑在 A 库、应用连 B 库"。"""
    explicit = os.getenv("ALEMBIC_DATABASE_URL", "").strip()
    if explicit:
        return explicit
    from app.core.config import get_settings

    dsn = get_settings().postgres_dsn.strip()
    if dsn:
        return dsn
    db_path = os.getenv("APP_DB_PATH", "").strip() or str(BACKEND / "data" / "app.db")
    pathlib.Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{db_path}"


async def _existing_tables(url: str) -> set[str]:
    from sqlalchemy import inspect
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            names = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())
        return set(names or [])
    finally:
        await engine.dispose()


async def _create_missing_tables(url: str) -> None:
    """幂等建表：只创建缺失的表，不动已有表（create_all 的既有语义）。"""
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.db.pg_models import Base

    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


def _alembic_config():
    from alembic.config import Config

    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    return cfg


def main() -> int:
    if not _enabled():
        print("[migrate] AUTO_MIGRATE 已关闭，跳过 schema 演进")
        return 0

    url = _database_url()
    safe = url.split("@")[-1]  # 只打印主机/路径，绝不打凭据
    print(f"[migrate] 目标库：{safe}")

    try:
        tables = asyncio.run(_existing_tables(url))
    except Exception as exc:  # noqa: BLE001 - 连不上库就是启动失败
        print(f"[migrate] 无法连接数据库：{exc}", file=sys.stderr)
        return 1

    versioned = "alembic_version" in tables
    has_schema = bool(tables - {"alembic_version"})
    cfg = _alembic_config()

    from alembic import command

    if has_schema and not versioned:
        # 存量库（create_all 建的）——两步走，缺一不可：
        # ① 先幂等建**缺失的表**：只 stamp 不建表的话，认领后 upgrade 变成空操作，
        #    像 audit_logs / mistake_notebook 这类新表永远不会被创建
        #    （运行时虽会再 create_all 兜住，但脚本不能假装"已对齐 head"）。
        # ② 再 stamp head 认领：库里已有基线结构，从基线重跑必撞 table already exists。
        # 注意：既有表**缺失的增量列**不归这里管——由运行期 `_SCHEMA_PATCHES` 补齐，
        # 这是本项目"两条建表路径收敛"的既定分工。
        print(f"[migrate] 检测到未纳管的存量 schema（{len(tables)} 张表，无 alembic_version）")
        try:
            asyncio.run(_create_missing_tables(url))
        except Exception as exc:  # noqa: BLE001
            print(f"[migrate] 补齐缺失表失败：{exc}", file=sys.stderr)
            return 1
        print("[migrate] 已补齐模型新增的表；执行 alembic stamp head 认领现有结构")
        try:
            command.stamp(cfg, "head")
        except Exception as exc:  # noqa: BLE001
            print(f"[migrate] stamp 失败：{exc}", file=sys.stderr)
            return 1
    elif not tables:
        print("[migrate] 空库：将按迁移链从基线建表")

    try:
        command.upgrade(cfg, "head")
    except Exception as exc:  # noqa: BLE001
        print(f"[migrate] upgrade head 失败：{exc}", file=sys.stderr)
        return 1

    print("[migrate] schema 已对齐 head")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
