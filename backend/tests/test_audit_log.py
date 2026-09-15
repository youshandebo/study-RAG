# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""审计日志测试：谁改的、脱敏在写入前、且审计本身不可被改写。

三条最该被钉住的断言
--------------------
1. **脱敏发生在写入前**：事后脱敏等于库里已存过明文，二次泄露面已经形成。
2. **没有任何 update/delete 代码路径**：能被改写的日志在合规场景里不存在。
3. **审计失败不反向击垮业务**：DB 抛错时 `record()` 必须静默返回而不是把
   用户的上传/充值操作一起带崩。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import app.db.relational as rel
from app.services.ops import audit


@pytest.fixture(autouse=True)
def clean_audit():
    audit.reset_memory()
    yield
    audit.reset_memory()


@pytest.fixture
def memory_audit(monkeypatch):
    """强制走进程内兜底路径。

    不切断 DB 会话的话，`record()` 会写进本机默认 SQLite 文件——
    用例之间互相污染，且"兜底路径"根本没被验证。
    """

    async def _no_db():
        return None

    monkeypatch.setattr(audit.repo, "db_session", _no_db)
    return audit


# --------------------------------------------------------- 只追加 / 脱敏 ----
def test_module_exposes_no_mutation_api():
    """只追加是设计要求，不是"暂时没做"——所以直接断言不存在修改能力。"""
    for forbidden in ("update", "delete", "remove", "edit", "purge"):
        assert not hasattr(audit, forbidden), f"审计模块不应提供 {forbidden}()"


@pytest.mark.asyncio
async def test_detail_is_redacted_before_write(memory_audit):
    """写入前脱敏：落库/落内存的内容里不得出现原始密钥。"""
    await audit.record(
        "admin.config.update",
        actor="platform_admin",
        detail={"api_key": "sk-live-abcdef1234567890", "note": "换了个中转"},
    )

    events = await audit.list_events()
    assert len(events) == 1
    stored = events[0]["detail"]
    assert "sk-live-abcdef1234567890" not in stored, "原始密钥不得落库"
    # 就地替换为标记（而非整段丢弃）：审计要能看出"这里原本有值、被保护了"
    assert "REDACTED" in stored.upper(), "应当是掩码替换，而不是整段丢弃"


@pytest.mark.asyncio
async def test_fingerprint_does_not_leak_token():
    token = "eyJhbGciOiJIUzI1NiJ9.payload.signature"
    fp = audit.fingerprint(token)

    assert fp and fp != token and token not in fp
    assert len(fp) == 12
    # 同一 token 稳定，不同 token 不同 → 可用于关联同会话操作
    assert audit.fingerprint(token) == fp
    assert audit.fingerprint("another-token") != fp


@pytest.mark.asyncio
async def test_record_admin_uses_fingerprint_not_raw_token(memory_audit):
    await audit.record_admin("ops.tenant.topup", token="secret-admin-token", tenant_id="org-a")

    event = (await audit.list_events())[0]
    assert event["actor"] == "platform_admin"
    assert event["actor_role"] == "platform_admin"
    assert "secret-admin-token" not in event["detail"]
    assert audit.fingerprint("secret-admin-token") in event["detail"]


@pytest.mark.asyncio
async def test_record_survives_storage_failure(monkeypatch):
    """审计是旁路：写不进去只记 WARNING，绝不能把主业务一起带崩。"""

    async def boom():
        raise RuntimeError("数据库连接池已耗尽")

    monkeypatch.setattr(audit.repo, "db_session", boom)
    await audit.record("ingest.upload", actor="u-1")  # 不抛异常即通过


# ------------------------------------------------------------- 查询过滤 ----
@pytest.mark.asyncio
async def test_filters_by_action_tenant_and_actor(memory_audit):
    await audit.record("ingest.upload", actor="u-1", tenant_id="org-a")
    await audit.record("ingest.upload", actor="u-2", tenant_id="org-b")
    await audit.record("ops.tenant.topup", actor="platform_admin", tenant_id="org-a")

    assert len(await audit.list_events()) == 3
    assert len(await audit.list_events(tenant_id="org-a")) == 2
    assert len(await audit.list_events(action="ingest.upload")) == 2
    assert len(await audit.list_events(actor="u-2")) == 1


@pytest.mark.asyncio
async def test_newest_first_and_pagination(memory_audit):
    for i in range(5):
        await audit.record("ingest.upload", actor=f"u-{i}")

    rows = await audit.list_events(limit=2)
    assert [r["actor"] for r in rows] == ["u-4", "u-3"], "审计天然按时间倒序看最近发生了什么"

    page2 = await audit.list_events(limit=2, offset=2)
    assert [r["actor"] for r in page2] == ["u-2", "u-1"]


# --------------------------------------------------------------- API 层 ----
@pytest.fixture
def db_env(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "audit.db"))
    monkeypatch.setenv("MULTI_TENANT_MODE", "1")
    saved = (rel._engine, rel._sessionmaker, rel._tables_ready, rel._pg_broken)
    rel._engine, rel._sessionmaker, rel._tables_ready, rel._pg_broken = None, None, False, False
    yield
    try:
        if rel._engine is not None:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(rel._engine.dispose())
            finally:
                loop.close()
    except Exception:
        pass
    rel._engine, rel._sessionmaker, rel._tables_ready, rel._pg_broken = saved


def _client():
    from fastapi.testclient import TestClient

    from app.api.v1 import admin
    from app.main import app

    admin._admin_limiter._hits.clear()  # 避免用例间累计触发 429
    return TestClient(app)


def _admin_token() -> str:
    from app.core.security import jwt_sign

    return jwt_sign({"role": "admin"}, 600)


def test_audit_endpoint_requires_admin(db_env):
    resp = _client().get("/api/v1/admin/ops/audit")
    assert resp.status_code == 401, "审计日志是最敏感的数据之一，未授权一律拒绝"


def test_topup_writes_audit_with_actor(db_env):
    client = _client()
    headers = {"X-Admin-Token": _admin_token()}

    resp = client.post(
        "/api/v1/admin/ops/tenants/org-a/topup",
        json={"amount": 5000, "memo": "年度合同", "idempotency_key": "batch-1"},
        headers=headers,
    )
    assert resp.status_code == 200

    listed = client.get("/api/v1/admin/ops/audit", headers=headers).json()
    assert listed["count"] == 1
    event = listed["items"][0]
    assert event["action"] == "ops.tenant.topup"
    assert event["actor"] == "platform_admin"
    assert event["tenant_id"] == "org-a"
    assert "5000" in event["detail"]
    assert "年度合同" in event["detail"]


def test_audit_endpoint_filters_and_csv_export(db_env):
    client = _client()
    headers = {"X-Admin-Token": _admin_token()}
    client.post(
        "/api/v1/admin/ops/tenants/org-a/topup",
        json={"amount": 100, "idempotency_key": "k1"},
        headers=headers,
    )
    client.post(
        "/api/v1/admin/ops/tenants/org-b/topup",
        json={"amount": 200, "idempotency_key": "k2"},
        headers=headers,
    )

    only_a = client.get("/api/v1/admin/ops/audit?tenant_id=org-a", headers=headers).json()
    assert only_a["count"] == 1
    assert only_a["items"][0]["tenant_id"] == "org-a"

    csv_resp = client.get("/api/v1/admin/ops/audit?format=csv", headers=headers)
    assert csv_resp.status_code == 200
    assert "ops.tenant.topup" in csv_resp.text
    assert "action" in csv_resp.text.splitlines()[0]


def test_role_change_is_audited(db_env):
    """提权/降权决定"谁能看到机构数据"，必须留痕。"""
    import app.db.relational as repo

    client = _client()
    headers = {"X-Admin-Token": _admin_token()}

    async def _mk_user() -> str:
        user = await repo.create_user("admin@org-a.test", "pw123456", tenant_id="org-a")
        return user["id"]

    user_id = asyncio.new_event_loop().run_until_complete(_mk_user())
    resp = client.post(
        f"/api/v1/admin/ops/users/{user_id}/role",
        json={"role": "tenant_admin"},
        headers=headers,
    )
    assert resp.status_code == 200

    items = client.get("/api/v1/admin/ops/audit", headers=headers).json()["items"]
    assert items[0]["action"] == "ops.user.role"
    assert items[0]["target"] == user_id
    assert "tenant_admin" in items[0]["detail"]
