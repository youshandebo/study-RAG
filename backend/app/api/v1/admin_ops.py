# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""平台运营后台最小交付（P1-C）：租户 CRUD / 充值流水 / FinOps 聚合 / 全量 bad-case。

权限：全部走 `admin.require_admin`（管理口令换的 admin JWT）——
只有**平台超管**能建租户、充值和跨租户排查。租户管理员走
`api/v1/feedback.py` 的自服务导出，且只能看到自己租户。

刻意不做的部分：不做图表、不做前端页面。这一层只提供**可被对账的数据出口**，
运营页面是纯展示层，随时可以换；数据口径错了才是灾难。
"""
from __future__ import annotations

import csv
import io
import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.v1.admin import require_admin
from app.core.billing import get_ledger
from app.core.circuit import snapshot as circuit_snapshot
from app.core.tenancy import is_valid_tenant
from app.services.ops import audit as audit_log
from app.services.ops import store

router = APIRouter()

_USAGE_COLUMNS = (
    "date", "tenant_id", "provider", "model", "calls",
    "prompt_tokens", "completion_tokens", "credits", "degraded_calls", "cost_usd",
)
_FEEDBACK_COLUMNS = (
    "created_at", "tenant_id", "trace_id", "message_id", "session_id", "source",
    "verdict", "error_code", "tags", "model", "degraded", "query", "retrieved", "note",
)


# ------------------------------------------------------------------ models --
class TenantCreate(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=64)
    label: str = ""
    tier_override: str | None = None
    daily_cap_override: int | None = None


class TenantPatch(BaseModel):
    label: str | None = None
    tier_override: str | None = None
    daily_cap_override: int | None = None
    frozen: bool | None = None


class TopUpBody(BaseModel):
    amount: int                 # 正数充值 / 负数人工调减
    memo: str = ""              # 合同款 / 赠送 / 测试 …
    idempotency_key: str = ""   # 防后台双击重复充值


class RoleBody(BaseModel):
    role: str = "member"        # member | tenant_admin


# ------------------------------------------------------------------ utils --
def _csv_response(filename: str, columns: tuple[str, ...], rows: list[dict]) -> dict:
    """生成 CSV 文本（带 UTF-8 BOM，Excel 直接打开不乱码）。

    返回 dict 由路由封装成响应；这里不直接返回 Response，方便测试断言内容。
    """
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(columns), extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    return {"filename": filename, "content": "﻿" + buf.getvalue()}


def _as_csv(content: str):
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=export.csv"},
    )


def _fmt_time(ms: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ms / 1000)) if ms else ""


# --------------------------------------------------------------- tenants --
@router.get("/admin/ops/tenants")
async def list_tenants(_: str = Depends(require_admin)):
    tenants = await store.list_tenants()
    ledger = get_ledger()
    for t in tenants:
        key = store.tenant_account_key(t["tenant_id"])
        t["daily_credits_spent"] = ledger.daily_spent(key)
        t["ledger_balance"] = ledger.balance(key)
    return {"tenants": tenants}


@router.post("/admin/ops/tenants")
async def create_tenant(body: TenantCreate, request: Request, token: str = Depends(require_admin)):
    if not is_valid_tenant(body.tenant_id):
        raise HTTPException(status_code=400, detail="租户 id 只允许字母数字、下划线与短横线（1-64 位）")
    acc = await store.ensure_tenant(body.tenant_id, body.label)
    if body.tier_override or body.daily_cap_override is not None:
        acc = await store.update_tenant(
            body.tenant_id,
            tier_override=body.tier_override,
            daily_cap_override=body.daily_cap_override,
        )
    # 体验额度：新机构如果没有这笔钱，成员第一次提问就被 402 拦下——
    # "机构已建好却开箱不可用"是最伤交付的死锁（充值要平台超管手动做，
    # 演示时没人会先去充）。一次性发放，幂等靠流水唯一键，多副本也只会入账一笔。
    trial = await store.grant_trial_quota(body.tenant_id)
    await audit_log.record_admin(
        "ops.tenant.create", token=token,
        ip=audit_log.ip_of(request),
        tenant_id=body.tenant_id, target=body.tenant_id,
        detail={"label": body.label, "tier_override": body.tier_override,
                "daily_cap_override": body.daily_cap_override,
                "trial_granted": trial["granted"]},
    )
    return {"tenant": acc, "trial": trial}


@router.patch("/admin/ops/tenants/{tenant_id}")
async def patch_tenant(
    tenant_id: str, body: TenantPatch, request: Request, token: str = Depends(require_admin)
):
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    if not patch:
        raise HTTPException(status_code=400, detail="没有需要更新的字段")
    acc = await store.update_tenant(tenant_id, **patch)
    await audit_log.record_admin(
        "ops.tenant.update", token=token,
        ip=audit_log.ip_of(request),
        tenant_id=tenant_id, target=tenant_id, detail=patch,
    )
    return {"tenant": acc}


@router.post("/admin/ops/tenants/{tenant_id}/topup")
async def top_up(
    tenant_id: str, body: TopUpBody, request: Request, token: str = Depends(require_admin)
):
    """充值 / 人工调减，写**不可变流水**并同步计费账本。

    幂等键由调用方给（后台通常用「租户+金额+操作批次」），双击重试只会入账一次。
    """
    try:
        result = await store.top_up(
            tenant_id, body.amount,
            operator_id="platform", idempotency_key=body.idempotency_key.strip() or "",
            memo=body.memo,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 充值已有不可变流水（钱的部分），审计补的是**操作主体**：
    # 流水能回答"调了多少"，审计才能回答"谁批的"。重复请求也要记，
    # 因为"某人重复点了 5 次"本身就是需要被看到的运营事实。
    await audit_log.record_admin(
        "ops.tenant.topup", token=token,
        ip=audit_log.ip_of(request),
        tenant_id=tenant_id, target=tenant_id,
        detail={"amount": body.amount, "duplicated": result["duplicated"],
                "balance_after": result["txn"].get("balance_after"),
                "memo": body.memo, "idempotency_key": body.idempotency_key.strip()},
    )
    return {
        "tenant_id": tenant_id,
        "granted_total": result["granted_total"],
        "duplicated": result["duplicated"],
        "txn": result["txn"],
    }


@router.get("/admin/ops/tenants/{tenant_id}/txns")
async def tenant_txns(tenant_id: str, limit: int = 200, _: str = Depends(require_admin)):
    return {"tenant_id": tenant_id, "txns": await store.list_txns(tenant_id, limit=limit)}


@router.post("/admin/ops/users/{user_id}/role")
async def set_role(
    user_id: str, body: RoleBody, request: Request, token: str = Depends(require_admin)
):
    """授予/收回租户管理员（只看自己租户数据的权限，不等于平台超管）。"""
    import app.db.relational as repo

    ok = await repo.set_user_role(user_id, body.role)
    if not ok:
        raise HTTPException(status_code=404, detail="用户不存在")
    # 提权/降权是审计里最敏感的一类：它决定"谁能看到机构数据"
    await audit_log.record_admin(
        "ops.user.role", token=token,
        ip=audit_log.ip_of(request),
        target=user_id, detail={"role": body.role},
    )
    return {"user_id": user_id, "role": body.role}


# ----------------------------------------------------------------- FinOps --
@router.get("/admin/ops/usage")
async def usage_metrics(
    tenant_id: str = "",
    days: int = 7,
    granularity: str = "day",       # day（对账口径）| hour（排查用量突刺）
    format: str = "json",
    _: str = Depends(require_admin),
):
    """按 (日期 × 供应商/模型) 聚合用量与成本，可导出 CSV。

    三个数据出口在此汇合：
    - token 与成本：`usage_events`（本表按次记录）
    - 平台额度扣减：计费账本 `daily_spent()`（跨副本共享，含在途冻结）
    - 供应商健康度：断路器 `snapshot()`（跳闸次数与冷却剩余）
    """
    days = max(1, min(90, days))
    granularity = granularity if granularity in ("day", "hour") else "day"
    since_ms = int((time.time() - days * 86400) * 1000)
    rows = await store.usage_rollup(tenant_id or None, since_ms, granularity=granularity)

    ledger = get_ledger()
    tenants = [tenant_id] if tenant_id else [t["tenant_id"] for t in await store.list_tenants()]
    daily_credits = {t: ledger.daily_spent(store.tenant_account_key(t)) for t in tenants}

    if format.lower() == "csv":
        csv_doc = _csv_response("usage.csv", _USAGE_COLUMNS, rows)
        return _as_csv(csv_doc["content"])
    return {
        "window_days": days,
        "granularity": granularity,
        "rows": rows,
        "daily_credits": daily_credits,
        "circuit": circuit_snapshot(),
    }


@router.get("/admin/ops/badcases")
async def badcases(
    tenant_id: str = "",
    days: int = 7,
    verdict: str = "",
    format: str = "json",
    _: str = Depends(require_admin),
):
    """全平台 bad-case 排查（平台超管专用；租户侧走 /api/v1/ops/badcases）。"""
    days = max(1, min(90, days))
    since_ms = int((time.time() - days * 86400) * 1000)
    rows = await store.list_feedback(tenant_id or None, since_ms=since_ms, verdict=verdict)

    if format.lower() == "csv":
        flat = [
            {**r, "tags": ",".join(r.get("tags") or []),
             "retrieved": json.dumps(r.get("retrieved") or [], ensure_ascii=False),
             "created_at": _fmt_time(r.get("created_at", 0))}
            for r in rows
        ]
        return _as_csv(_csv_response("badcases.csv", _FEEDBACK_COLUMNS, flat)["content"])
    return {"window_days": days, "count": len(rows), "items": rows}


@router.get("/admin/ops/circuit")
async def circuit_state(_: str = Depends(require_admin)):
    """各上游熔断状态：跳闸次数、当前状态、冷却剩余秒数。

    LLM 与 Embedding 通道共用同一份注册表，所以这里能同时看到两类上游的健康度。
    """
    return {"breakers": circuit_snapshot()}


# ------------------------------------------------------------------ 审计 ----
_AUDIT_COLUMNS = ("ts", "action", "actor", "actor_role", "tenant_id", "target", "ip", "detail")


@router.get("/admin/ops/audit")
async def list_audit(
    tenant_id: str = "",
    action: str = "",
    actor: str = "",
    limit: int = 200,
    offset: int = 0,
    format: str = "json",
    _: str = Depends(require_admin),
):
    """敏感操作审计（只读）。

    与业务结果表的分工：`txns` 回答"钱怎么变的"，本接口回答"**谁**改的"。
    只提供读取——审计表没有任何 update/delete 代码路径，能被改写的日志
    在合规场景里等于不存在。
    """
    rows = await audit_log.list_events(
        tenant_id=tenant_id, action=action, actor=actor, limit=limit, offset=offset,
    )
    if format == "csv":
        flat = [
            {
                "ts": _fmt_time(r["ts"]), "action": r["action"], "actor": r["actor"],
                "actor_role": r["actor_role"], "tenant_id": r["tenant_id"],
                "target": r["target"], "ip": r["ip"], "detail": r["detail"],
            }
            for r in rows
        ]
        return _as_csv(_csv_response("audit_logs.csv", _AUDIT_COLUMNS, flat)["content"])
    return {"count": len(rows), "items": rows}
