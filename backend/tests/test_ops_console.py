# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""P1-C 运营后台核心测试：充值幂等流水 + 跨租户 bad-case 隔离 + 导出脱敏。

运行：cd backend && python -m pytest tests/test_ops_console.py -q

两条最该被钉住的断言
--------------------
1. **同一幂等键绝不重复入账**：后台双击是常态，"先查再写"在并发下必然漏，
   所以这里既测"重复调用返回首笔"，也断言唯一索引**真实存在**。
2. **租户管理员拿不到别人的 bad-case**：租户值一律由服务端解析，
   接口不接受客户端传 tenant_id——这条要测到 API 层，而不是只测数据层。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import app.db.relational as rel
from app.core.billing import get_ledger, reset_ledger
from app.services.ops import store


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    """每个用例一套独立库 + 干净账本，互不污染。"""
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "ops.db"))
    monkeypatch.setenv("MULTI_TENANT_MODE", "1")
    saved = (rel._engine, rel._sessionmaker, rel._tables_ready, rel._pg_broken)
    rel._engine, rel._sessionmaker, rel._tables_ready, rel._pg_broken = None, None, False, False

    store.reset_memory()
    reset_ledger()
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
    store.reset_memory()
    reset_ledger()


def _user(tenant: str, role: str = "member", anonymous: bool = False):
    from app.api.v1.auth import AuthUser

    return AuthUser("u-1", "a@b.com", "free", anonymous=anonymous, tenant_id=tenant, role=role)


# ------------------------------------------------------------ 充值台账 ----
class TestTopUpLedger:
    @pytest.mark.asyncio
    async def test_top_up_writes_immutable_txn(self, db_env):
        res = await store.top_up("org-a", 1000, operator_id="ops-1", memo="合同款")

        assert res["duplicated"] is False and res["granted_total"] == 1000
        txn = res["txn"]
        assert txn["amount"] == 1000 and txn["balance_before"] == 0 and txn["balance_after"] == 1000
        assert txn["operator_id"] == "ops-1" and txn["memo"] == "合同款"

        txns = await store.list_txns("org-a")
        assert len(txns) == 1 and txns[0]["id"] == txn["id"]

    @pytest.mark.asyncio
    async def test_idempotent_key_prevents_double_credit(self, db_env):
        """后台双击：第二笔必须被识别为重复，且**余额不变**"""
        first = await store.top_up("org-a", 1000, idempotency_key="batch-1")
        again = await store.top_up("org-a", 1000, idempotency_key="batch-1")

        assert again["duplicated"] is True
        assert again["txn"]["id"] == first["txn"]["id"]
        assert again["granted_total"] == 1000, "重复充值不应改变余额"
        assert len(await store.list_txns("org-a")) == 1, "流水只应有一条"

    @pytest.mark.asyncio
    async def test_distinct_keys_credit_twice(self, db_env):
        await store.top_up("org-a", 1000, idempotency_key="batch-1")
        await store.top_up("org-a", 500, idempotency_key="batch-2")

        assert (await store.get_tenant("org-a"))["granted_total"] == 1500
        assert len(await store.list_txns("org-a")) == 2

    @pytest.mark.asyncio
    async def test_negative_adjustment_supported(self, db_env):
        await store.top_up("org-a", 1000)
        res = await store.top_up("org-a", -200, memo="合同变更调减")

        assert res["granted_total"] == 800
        assert res["txn"]["amount"] == -200
        assert res["txn"]["balance_before"] == 1000 and res["txn"]["balance_after"] == 800

    @pytest.mark.asyncio
    async def test_zero_amount_rejected(self, db_env):
        with pytest.raises(ValueError):
            await store.top_up("org-a", 0)

    def test_idempotency_key_has_unique_index(self):
        """并发双击的最后一道闸是唯一索引，不是"先查一遍"。"""
        from app.db.pg_models import BalanceTxnRow

        assert BalanceTxnRow.__table__.c.idempotency_key.unique is True

    @pytest.mark.asyncio
    async def test_top_up_syncs_tenant_ledger_account(self, db_env):
        """发放的额度必须落到租户账户上——成员消费的是这本账。"""
        await store.top_up("org-a", 2000)
        assert get_ledger().balance(store.tenant_account_key("org-a")) == 2000

        await store.top_up("org-a", -500)
        assert get_ledger().balance(store.tenant_account_key("org-a")) == 1500


# ------------------------------------------------------------ 租户配置 ----
class TestTenantControls:
    @pytest.mark.asyncio
    async def test_freeze_and_cap_override(self, db_env):
        await store.update_tenant("org-a", frozen=True, daily_cap_override=5000)
        ctx = await store.tenant_billing_context("org-a")

        assert ctx["frozen"] is True
        assert ctx["daily_cap_override"] == 5000

    @pytest.mark.asyncio
    async def test_unknown_tenant_is_not_frozen(self, db_env):
        ctx = await store.tenant_billing_context("org-unknown")
        assert ctx["frozen"] is False and ctx["daily_cap_override"] is None


# -------------------------------------------------------------- FinOps ----
class TestUsageRollup:
    @pytest.mark.asyncio
    async def test_groups_by_day_provider_and_model(self, db_env):
        for i in range(2):
            await store.record_usage(tenant_id="org-a", provider="main", model="m-1",
                                     prompt_tokens=100, completion_tokens=50, degraded=(i == 0))
        await store.record_usage(tenant_id="org-a", provider="fallback:deepseek", model="m-2",
                                 prompt_tokens=200, completion_tokens=100, degraded=True)
        await store.record_usage(tenant_id="org-b", provider="main", model="m-1",
                                 prompt_tokens=10, completion_tokens=5)

        rows = await store.usage_rollup("org-a")
        assert len(rows) == 2, rows

        by_model = {r["model"]: r for r in rows}
        assert by_model["m-1"]["calls"] == 2
        assert by_model["m-1"]["prompt_tokens"] == 200
        assert by_model["m-1"]["degraded_calls"] == 1
        assert by_model["m-2"]["provider"] == "fallback:deepseek"
        assert by_model["m-2"]["degraded_calls"] == 1
        assert by_model["m-1"]["cost_usd"] > 0, "应给出成本估算"

    @pytest.mark.asyncio
    async def test_hour_granularity_for_spike_hunting(self, db_env):
        """小时桶用于排查用量突刺；非法粒度回落到 day，不能报错也不能静默乱分桶。"""
        await store.record_usage(tenant_id="org-a", provider="main", model="m-1", prompt_tokens=10)

        hourly = await store.usage_rollup("org-a", granularity="hour")
        assert hourly and ":" in hourly[0]["date"], hourly

        fallback = await store.usage_rollup("org-a", granularity="秒")
        assert fallback and ":" not in fallback[0]["date"]

    @pytest.mark.asyncio
    async def test_platform_rollup_spans_tenants(self, db_env):
        """全平台聚合按租户分桶：成本要能归因到具体机构，不能混成一坨。"""
        await store.record_usage(tenant_id="org-a", provider="main", model="m-1", prompt_tokens=10)
        await store.record_usage(tenant_id="org-b", provider="main", model="m-1", prompt_tokens=20)

        rows = await store.usage_rollup()
        assert {r["tenant_id"] for r in rows} == {"org-a", "org-b"}
        assert [r["prompt_tokens"] for r in sorted(rows, key=lambda r: r["tenant_id"])] == [10, 20]


# ------------------------------------------------------------ bad-case ----
class TestBadCaseIsolation:
    @staticmethod
    async def _seed():
        await store.record_feedback(tenant_id="org-a", message_id="m-a", query="A 的问题",
                                    retrieved=[{"chunk_id": "c-1", "score": 0.9}], verdict="down",
                                    tags=["答案不准"], source="explicit")
        await store.record_feedback(tenant_id="org-b", message_id="m-b", query="B 的机密问题",
                                    verdict="down", source="explicit")

    @pytest.mark.asyncio
    async def test_store_level_isolation(self, db_env):
        await self._seed()
        rows = await store.list_feedback("org-a")
        assert [r["message_id"] for r in rows] == ["m-a"], "数据层串租户了"

    @pytest.mark.asyncio
    async def test_tenant_admin_only_sees_own_tenant(self, db_env):
        """API 层：租户管理员导出时作用域被强制注入，拿不到别人家数据。"""
        from app.api.v1.feedback import export_badcases

        await self._seed()
        resp = await export_badcases(days=7, verdict="", format="json", user=_user("org-a", "tenant_admin"))

        assert resp["tenant_id"] == "org-a"
        assert [i["message_id"] for i in resp["items"]] == ["m-a"]
        assert all(i["tenant_id"] == "org-a" for i in resp["items"])

    @pytest.mark.asyncio
    async def test_member_cannot_export(self, db_env):
        from fastapi import HTTPException

        from app.api.v1.feedback import export_badcases

        with pytest.raises(HTTPException) as ei:
            await export_badcases(days=7, verdict="", format="json", user=_user("org-a", "member"))
        assert ei.value.status_code == 403

    @pytest.mark.asyncio
    async def test_anonymous_cannot_export(self, db_env):
        from fastapi import HTTPException

        from app.api.v1.feedback import export_badcases

        with pytest.raises(HTTPException) as ei:
            await export_badcases(days=7, verdict="", format="json", user=_user("org-a", anonymous=True))
        assert ei.value.status_code == 401

    @pytest.mark.asyncio
    async def test_csv_export_has_bom_and_header(self, db_env):
        """CSV 要能被客户直接用 Excel 打开：带 BOM，且列名齐全。"""
        from fastapi.responses import Response

        from app.api.v1.feedback import export_badcases

        await self._seed()
        resp = await export_badcases(days=7, verdict="", format="csv", user=_user("org-a", "tenant_admin"))

        assert isinstance(resp, Response)
        body = resp.body.decode("utf-8")
        assert body.startswith("﻿"), "缺 UTF-8 BOM，Excel 打开中文会乱码"
        assert "tenant_id" in body.splitlines()[0]
        assert "B 的机密问题" not in body, "CSV 里出现了其他租户的数据"

    @pytest.mark.asyncio
    async def test_platform_admin_can_cross_tenant(self, db_env):
        from app.api.v1.admin_ops import badcases

        await self._seed()
        resp = await badcases(tenant_id="", days=7, verdict="", format="json", _="tok")
        assert resp["count"] == 2, "平台超管应能跨租户排查"


# -------------------------------------------------------------- 反馈 ----
class TestFeedbackIngest:
    @pytest.mark.asyncio
    async def test_query_is_redacted_on_write(self, db_env):
        """脱敏必须在**写入时**发生——库里就不该躺着明文凭证。"""
        from app.api.v1.feedback import FeedbackBody, submit_feedback

        await submit_feedback(
            FeedbackBody(message_id="m-1", verdict="down", query="我的 key 是 sk-abcdef1234567890，手机 13800138000",
                         tags=["答案不准"], note="联系我 a@b.com"),
            _user("org-a", "member"),
        )
        rows = await store.list_feedback("org-a")
        stored = rows[0]["query"]

        assert "sk-abcdef1234567890" not in stored
        assert "13800138000" not in stored
        assert "[REDACTED_KEY]" in stored

    @pytest.mark.asyncio
    async def test_verdict_must_be_up_or_down(self, db_env):
        from fastapi import HTTPException

        from app.api.v1.feedback import FeedbackBody, submit_feedback

        with pytest.raises(HTTPException) as ei:
            await submit_feedback(FeedbackBody(message_id="m-1", verdict="maybe"), _user("org-a", "member"))
        assert ei.value.status_code == 400

    @pytest.mark.asyncio
    async def test_tenant_comes_from_server_not_client(self, db_env):
        """用户归属哪个租户由服务端解析，客户端说了不算。"""
        from app.api.v1.feedback import FeedbackBody, submit_feedback

        res = await submit_feedback(
            FeedbackBody(message_id="m-1", verdict="down", query="x"), _user("org-zzz", "member")
        )
        assert res["tenant_id"] == "org-zzz"


# ------------------------------------------------------------ 平台接口 ----
class TestPlatformEndpoints:
    @pytest.mark.asyncio
    async def test_usage_endpoint_merges_three_sources(self, db_env):
        from app.api.v1.admin_ops import usage_metrics

        await store.top_up("org-a", 5000)
        await store.record_usage(tenant_id="org-a", provider="main", model="m-1", prompt_tokens=100)
        out = await usage_metrics(tenant_id="org-a", days=7, format="json", _="tok")

        assert out["rows"], "缺 token 聚合"
        assert "org-a" in out["daily_credits"], "缺平台额度扣减（daily_spent 出口）"
        assert isinstance(out["circuit"], list), "缺断路器快照"

    @pytest.mark.asyncio
    async def test_top_up_endpoint_is_idempotent(self, db_env):
        from app.api.v1.admin_ops import TopUpBody, top_up

        first = await top_up("org-a", TopUpBody(amount=1000, idempotency_key="k-1"), "tok")
        again = await top_up("org-a", TopUpBody(amount=1000, idempotency_key="k-1"), "tok")

        assert first["duplicated"] is False
        assert again["duplicated"] is True and again["granted_total"] == 1000

    @pytest.mark.asyncio
    async def test_invalid_tenant_id_rejected(self, db_env):
        from fastapi import HTTPException

        from app.api.v1.admin_ops import TenantCreate, create_tenant

        with pytest.raises(HTTPException) as ei:
            await create_tenant(TenantCreate(tenant_id="坏 id/含斜杠"), "tok")
        assert ei.value.status_code == 400


# ----------------------------------------------- 新租户体验额度 ----
class TestTenantTrialQuota:
    """「新机构开箱即 402」是最伤交付的死锁：租户建好了，成员一问就被余额拦下。

    权威幂等判定必须是**流水唯一索引**（`trial:<tenant>`），不是"先查一遍余额"——
    后者在并发/多副本下会双发，等于凭空多发钱。
    """

    @pytest.mark.asyncio
    async def test_new_tenant_gets_trial_credit(self, db_env):
        from app.api.v1.admin_ops import TenantCreate, create_tenant

        out = await create_tenant(TenantCreate(tenant_id="org-new", label="新机构"), None, "tok")

        assert out["trial"]["granted"] == store.trial_credits() > 0
        assert get_ledger().balance(store.tenant_account_key("org-new")) == store.trial_credits()
        txns = await store.list_txns("org-new")
        assert len(txns) == 1
        assert txns[0]["idempotency_key"] == "trial:org-new"
        assert txns[0]["operator_id"] == store.TRIAL_OPERATOR

    @pytest.mark.asyncio
    async def test_trial_is_granted_at_most_once(self, db_env):
        from app.api.v1.admin_ops import TenantCreate, create_tenant

        await create_tenant(TenantCreate(tenant_id="org-new"), None, "tok")
        again = await create_tenant(TenantCreate(tenant_id="org-new"), None, "tok")

        assert again["trial"]["granted"] == 0 and again["trial"]["duplicated"] is True
        assert get_ledger().balance(store.tenant_account_key("org-new")) == store.trial_credits()
        assert len(await store.list_txns("org-new")) == 1, "体验额度只能有一笔流水"

    @pytest.mark.asyncio
    async def test_trial_can_be_disabled_by_env(self, db_env, monkeypatch):
        monkeypatch.setenv("TENANT_TRIAL_CREDITS", "0")

        res = await store.grant_trial_quota("org-x")

        assert res["skipped"] is True and res["granted"] == 0
        assert await store.list_txns("org-x") == []
        assert get_ledger().balance(store.tenant_account_key("org-x")) == 0

    @pytest.mark.asyncio
    async def test_trial_amount_is_configurable(self, db_env, monkeypatch):
        monkeypatch.setenv("TENANT_TRIAL_CREDITS", "1234")

        res = await store.grant_trial_quota("org-x")

        assert res["granted"] == 1234
        assert get_ledger().balance(store.tenant_account_key("org-x")) == 1234

    @pytest.mark.asyncio
    async def test_lazy_grant_heals_preexisting_tenant(self, db_env):
        """早于本功能创建的租户（或绕过创建接口隐式产生的租户）也要能自愈。"""
        await store.ensure_tenant("org-old", "历史机构")
        assert get_ledger().balance(store.tenant_account_key("org-old")) == 0

        res = await store.grant_trial_quota("org-old")

        assert res["granted"] == store.trial_credits()
        assert get_ledger().balance(store.tenant_account_key("org-old")) == store.trial_credits()

    @pytest.mark.asyncio
    async def test_trial_credits_survives_bad_env_value(self, db_env, monkeypatch):
        monkeypatch.setenv("TENANT_TRIAL_CREDITS", "not-a-number")

        assert store.trial_credits() == 50_000, "脏配置应按默认值处理而不是崩溃或归零"
