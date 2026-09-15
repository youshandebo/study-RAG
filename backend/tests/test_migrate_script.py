# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""容器启动迁移脚本（scripts/migrate.py）测试——must-fix 3。

要钉死的一条：**存量库必须先被 stamp 认领，否则 upgrade head 必撞 table already exists**。

本项目有两条建表路径：运行期 `create_all` 与 Alembic 迁移。老部署的库里有表、
但没有 `alembic_version`；此时直接从基线跑迁移会在第一句 CREATE TABLE 上失败。
这个用例用**真实 SQLite 文件 + 真实子进程**验证认领逻辑，而不是断言脚本文本。
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
MIGRATE = BACKEND / "scripts" / "migrate.py"


def _run(db: Path, **env_extra) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["APP_DB_PATH"] = str(db)
    env.pop("ALEMBIC_DATABASE_URL", None)
    env.pop("POSTGRES_DSN", None)      # 防止宿主机配置把用例指向真实库
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(MIGRATE)],
        cwd=str(BACKEND), capture_output=True, text=True, env=env, timeout=180,
    )


def _tables(db: Path) -> set[str]:
    con = sqlite3.connect(db)
    try:
        return {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    finally:
        con.close()


def _create_legacy_db(db: Path) -> None:
    """模拟 create_all 建起来的存量库：有业务表、无 alembic_version。"""
    con = sqlite3.connect(db)
    try:
        con.execute("create table sessions (id varchar(32) primary key, title varchar(200))")
        con.execute("create table users (id varchar(32) primary key, email varchar(200))")
        con.commit()
    finally:
        con.close()


def test_empty_database_gets_migrated_to_head(tmp_path):
    db = tmp_path / "fresh.db"

    res = _run(db)

    assert res.returncode == 0, res.stderr
    tables = _tables(db)
    assert "alembic_version" in tables
    assert "audit_logs" in tables, "迁移链应建出最新 revision 的表"
    con = sqlite3.connect(db)
    try:
        version = con.execute("select version_num from alembic_version").fetchone()[0]
    finally:
        con.close()
    assert version == "0006"


def test_legacy_database_is_adopted_not_rebuilt(tmp_path):
    """存量库：认领 + 保留既有数据（绝不能因为"表已存在"就失败或重建）。"""
    db = tmp_path / "legacy.db"
    _create_legacy_db(db)
    con = sqlite3.connect(db)
    con.execute("insert into sessions (id, title) values ('s-1', '老会话')")
    con.commit()
    con.close()

    res = _run(db)

    assert res.returncode == 0, res.stderr
    assert "认领" in res.stdout, "应显式声明走了 stamp 认领路径"

    tables = _tables(db)
    assert "alembic_version" in tables and "audit_logs" in tables
    # 存量数据必须还在：认领不是重建
    con = sqlite3.connect(db)
    try:
        assert con.execute("select title from sessions where id='s-1'").fetchone()[0] == "老会话"
    finally:
        con.close()


def test_second_run_is_idempotent(tmp_path):
    db = tmp_path / "twice.db"
    assert _run(db).returncode == 0

    second = _run(db)

    assert second.returncode == 0, second.stderr
    assert "认领" not in second.stdout, "已纳管的库不应再次 stamp"


def test_auto_migrate_off_skips_everything(tmp_path):
    db = tmp_path / "off.db"

    res = _run(db, AUTO_MIGRATE="0")

    assert res.returncode == 0
    assert "跳过" in res.stdout
    assert not db.exists() or "alembic_version" not in _tables(db)


def test_unreachable_database_fails_fast(tmp_path):
    """连不上库必须非 0 退出（让容器重启循环把问题暴露出来），而不是静默继续。"""
    res = _run(tmp_path / "x.db", ALEMBIC_DATABASE_URL="postgresql+asyncpg://nobody@127.0.0.1:1/none")

    assert res.returncode != 0
    assert "无法连接数据库" in res.stderr or "失败" in res.stderr
