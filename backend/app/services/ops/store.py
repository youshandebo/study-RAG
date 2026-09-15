# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""运营台账数据层：租户账户 / 充值流水 / 用量事件 / bad-case。

三条不可妥协的口径
------------------
1. **充值只追加、不改余额字段**：`granted_total` 只是"累计发过多少"的快照，
   真正的权威是 `balance_txns` 流水。只 UPDATE 余额等于没有审计轨迹，
   出现"充了但没到账 / 多充了"的对账纠纷时无从查证。
2. **幂等靠唯一索引，不靠"先查一遍"**：后台双击或前端重试会并发打到同一个
   幂等键，"先查再写"在并发下必然重复入账。唯一索引是唯一可靠的闸门。
3. **bad-case 按租户硬隔离**：租户管理员查任何接口都强制带上自己的 tenant_id，
   且**不接受客户端传入的 tenant 参数**（那是可伪造的越权口子）。

DB 不可用时回落进程内字典——单副本演示可用，多副本需配 POSTGRES_DSN。
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import app.db.relational as repo
from app.services.ops.redact import redact

_logger = logging.getLogger("app.services.ops.store")

_UTC8 = timezone(timedelta(hours=8))

# 计费账本里的租户账户键：与按人/按 IP 的账户（`u:` / `ip:`）区分开
TENANT_ACCOUNT_PREFIX = "t:"

# 进程内兜底（DB 不可用时）
_mem_tenants: dict[str, dict[str, Any]] = {}
_mem_txns: list[dict[str, Any]] = []
_mem_usage: list[dict[str, Any]] = []
_mem_feedback: list[dict[str, Any]] = []
# 体验额度已检查过的租户（只是省掉热路径上的重复查询，权威判定是流水唯一索引）
_mem_trial_checked: set[str] = set()


def _now_ms() -> int:
    return int(time.time() * 1000)


def tenant_account_key(tenant_id: str) -> str:
    return f"{TENANT_ACCOUNT_PREFIX}{tenant_id}"


def _day_of(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, _UTC8).strftime("%Y-%m-%d")


def bucket_of(ms: int, granularity: str = "day") -> str:
    """时间分桶：day → `2026-09-14`；hour → `2026-09-14 19:00`。

    统一走 UTC+8 切分，与日配额的"次日零点重置"口径一致——
    否则对账时会出现"统计说昨天用了、配额说今天用完了"这种解释不清的差异。
    """
    dt = datetime.fromtimestamp(ms / 1000, _UTC8)
    return dt.strftime("%Y-%m-%d %H:00" if granularity == "hour" else "%Y-%m-%d")


def reset_memory() -> None:
    """测试用：清空进程内兜底数据。"""
    _mem_tenants.clear()
    _mem_txns.clear()
    _mem_usage.clear()
    _mem_feedback.clear()
    # 体验额度的"已检查"标记也要清：否则下一个用例会以为该租户已发过
    _mem_trial_checked.clear()


# ------------------------------------------------------------ 价格估算 ----
def _price_table() -> dict[str, tuple[float, float]]:
    """每 1K token 的 (输入价, 输出价)，单位 USD。

    没有放之四海皆准的价目表——各家供应商随时调价。这里给一个保守默认值，
    部署方用 `OPS_PRICE_TABLE`（JSON）覆盖即可，别硬编码进代码。
    """
    import os

    table = {"default": (0.0015, 0.002)}
    raw = (os.getenv("OPS_PRICE_TABLE", "") or "").strip()
    if not raw:
        return table
    try:
        loaded = json.loads(raw)
        for name, pair in (loaded or {}).items():
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                table[str(name)] = (float(pair[0]), float(pair[1]))
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return table


def estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """按模型价目估算成本；未登记的型号回落 default 档。"""
    table = _price_table()
    pin, pout = table.get(model or "", table.get("default", (0.0, 0.0)))
    return round(prompt_tokens / 1000.0 * pin + completion_tokens / 1000.0 * pout, 6)


# ------------------------------------------------------------ 租户账户 ----
def _tenant_dict(r: Any) -> dict[str, Any]:
    return {
        "tenant_id": r.tenant_id,
        "label": r.label or "",
        "tier_override": r.tier_override,
        "daily_cap_override": r.daily_cap_override,
        "granted_total": int(r.granted_total or 0),
        "frozen": bool(r.frozen),
        "created_at": int(r.created_at or 0),
        "updated_at": int(r.updated_at or 0),
    }


async def get_tenant(tenant_id: str) -> dict[str, Any] | None:
    session = await repo.db_session()
    if session is not None:
        from app.db.pg_models import TenantAccountRow

        async with session:
            row = await session.get(TenantAccountRow, tenant_id)
        return _tenant_dict(row) if row else None
    return dict(_mem_tenants[tenant_id]) if tenant_id in _mem_tenants else None


async def ensure_tenant(tenant_id: str, label: str = "") -> dict[str, Any]:
    now = _now_ms()
    session = await repo.db_session()
    if session is not None:
        from app.db.pg_models import TenantAccountRow

        async with session:
            row = await session.get(TenantAccountRow, tenant_id)
            if row is None:
                row = TenantAccountRow(
                    tenant_id=tenant_id, label=label or "", granted_total=0,
                    frozen=0, created_at=now, updated_at=now,
                )
                session.add(row)
                await session.commit()
        return _tenant_dict(row)
    acc = _mem_tenants.get(tenant_id)
    if acc is None:
        acc = {
            "tenant_id": tenant_id, "label": label or "", "tier_override": None,
            "daily_cap_override": None, "granted_total": 0, "frozen": False,
            "created_at": now, "updated_at": now,
        }
        _mem_tenants[tenant_id] = acc
    return dict(acc)


async def list_tenants() -> list[dict[str, Any]]:
    session = await repo.db_session()
    if session is not None:
        from sqlalchemy import select

        from app.db.pg_models import TenantAccountRow

        async with session:
            rows = (await session.execute(select(TenantAccountRow))).scalars().all()
        return [_tenant_dict(r) for r in rows]
    return [dict(v) for v in _mem_tenants.values()]


async def update_tenant(tenant_id: str, **fields: Any) -> dict[str, Any] | None:
    """更新租户配置（档位覆盖 / 日上限覆盖 / 冻结 / 名称）。"""
    await ensure_tenant(tenant_id)
    allowed = {"label", "tier_override", "daily_cap_override", "frozen"}
    patch = {k: v for k, v in fields.items() if k in allowed}
    if "frozen" in patch:
        patch["frozen"] = 1 if patch["frozen"] else 0
    patch["updated_at"] = _now_ms()

    session = await repo.db_session()
    if session is not None:
        from sqlalchemy import update

        from app.db.pg_models import TenantAccountRow

        async with session:
            await session.execute(
                update(TenantAccountRow).where(TenantAccountRow.tenant_id == tenant_id).values(**patch)
            )
            await session.commit()
        return await get_tenant(tenant_id)
    acc = _mem_tenants.get(tenant_id)
    if acc is None:
        return None
    for k, v in patch.items():
        acc[k] = bool(v) if k == "frozen" else v
    return dict(acc)


# -------------------------------------------------------------- 充值 ----
def _txn_dict(r: Any) -> dict[str, Any]:
    return {
        "id": r.id, "tenant_id": r.tenant_id, "amount": int(r.amount),
        "balance_before": int(r.balance_before), "balance_after": int(r.balance_after),
        "operator_id": r.operator_id or "", "idempotency_key": r.idempotency_key,
        "memo": r.memo or "", "created_at": int(r.created_at or 0),
    }


async def _txn_by_idem(key: str) -> dict[str, Any] | None:
    session = await repo.db_session()
    if session is not None:
        from sqlalchemy import select

        from app.db.pg_models import BalanceTxnRow

        async with session:
            row = (await session.execute(
                select(BalanceTxnRow).where(BalanceTxnRow.idempotency_key == key)
            )).scalar_one_or_none()
        return _txn_dict(row) if row else None
    for t in _mem_txns:
        if t.get("idempotency_key") == key:
            return dict(t)
    return None


async def top_up(
    tenant_id: str,
    amount: int,
    *,
    operator_id: str = "",
    idempotency_key: str = "",
    memo: str = "",
) -> dict[str, Any]:
    """充值 / 人工调减，返回 `{txn, duplicated, granted_total}`。

    幂等：同一 `idempotency_key` 只会入账一次，重复调用返回首次流水且不改余额。
    并发安全由唯一索引兜底（catch IntegrityError 后回读），不是靠"先查再写"。
    """
    amount = int(amount)
    if amount == 0:
        raise ValueError("充值金额不能为 0")

    if idempotency_key:
        hit = await _txn_by_idem(idempotency_key)
        if hit is not None:
            return {"txn": hit, "duplicated": True, "granted_total": hit["balance_after"]}

    acct = await ensure_tenant(tenant_id)
    before = int(acct["granted_total"])
    after = before + amount
    now = _now_ms()
    txn = {
        "id": uuid.uuid4().hex[:16], "tenant_id": tenant_id, "amount": amount,
        "balance_before": before, "balance_after": after,
        "operator_id": operator_id, "idempotency_key": idempotency_key or None,
        "memo": memo, "created_at": now,
    }

    session = await repo.db_session()
    if session is not None:
        from sqlalchemy import update
        from sqlalchemy.exc import IntegrityError

        from app.db.pg_models import BalanceTxnRow, TenantAccountRow

        try:
            async with session:
                session.add(BalanceTxnRow(**txn))
                await session.execute(
                    update(TenantAccountRow)
                    .where(TenantAccountRow.tenant_id == tenant_id)
                    .values(granted_total=after, updated_at=now)
                )
                await session.commit()
        except IntegrityError:
            # 并发双击：唯一索引挡住了第二笔，回读首次流水即可
            hit = await _txn_by_idem(idempotency_key) if idempotency_key else None
            if hit is not None:
                return {"txn": hit, "duplicated": True, "granted_total": hit["balance_after"]}
            raise
    else:
        _mem_txns.append(dict(txn))
        _mem_tenants[tenant_id]["granted_total"] = after
        _mem_tenants[tenant_id]["updated_at"] = now

    _sync_ledger(tenant_id, amount)
    return {"txn": txn, "duplicated": False, "granted_total": after}


def _sync_ledger(tenant_id: str, amount: int) -> None:
    """把发放额度同步到计费账本：成员消费的是**租户**这本账。"""
    from app.core.billing import get_ledger

    account = tenant_account_key(tenant_id)
    try:
        if amount > 0:
            get_ledger().grant(account, amount)
        else:
            get_ledger().debit(account, -amount)
    except Exception:  # noqa: BLE001 - 账本不可用不该让台账写入失败
        _logger.warning("账本同步失败（台账已记账，需人工核对）: tenant=%s amount=%s", tenant_id, amount)


# ------------------------------------------------- 新租户体验额度 ----
# 「新注册机构开箱即 402」是最伤交付体验的一种死锁：租户刚建好、成员一点提问
# 就被余额拦下。根因是余额只由充值注入，而充值需要平台超管手动操作——
# 交付演示时没人会先去后台充一笔。这里给每个租户一次性体验额度，把死锁消除。
#
# 三个设计取舍：
# 1. **复用 top_up 的不可变流水**（唯一幂等键 `trial:<tenant>`），而不是直接
#    调 ledger.grant：这样额度有台账、可审计、可对账，且天然防双发
#    （并发/重试/多副本都由唯一索引兜底），不需要发明第二套幂等机制。
# 2. **幂等键不含时间戳**：体验额度是"一次性"而不是"每天一次"，否则等于
#    给每个租户无限免费额度，成本敞口不可控。
# 3. **进程内已检查集合**只是省掉热路径上的重复查询；权威判定始终是流水唯一索引
#    ——多副本下每个副本各查一次，但仍只会入账一笔。

TRIAL_OPERATOR = "system:trial"
TRIAL_MEMO = "新租户体验额度（系统一次性发放）"
_DEFAULT_TRIAL_CREDITS = 50_000


def trial_credits() -> int:
    """体验额度大小：`TENANT_TRIAL_CREDITS` 环境变量，默认 50000；0 表示关闭。"""
    raw = (os.getenv("TENANT_TRIAL_CREDITS") or "").strip()
    if not raw:
        return _DEFAULT_TRIAL_CREDITS
    try:
        return max(0, int(raw))
    except ValueError:
        _logger.warning("TENANT_TRIAL_CREDITS=%r 不是整数，按默认 %d 处理", raw, _DEFAULT_TRIAL_CREDITS)
        return _DEFAULT_TRIAL_CREDITS


async def grant_trial_quota(tenant_id: str, credits: int | None = None) -> dict[str, Any]:
    """为新租户发放一次性体验额度（幂等）。

    返回 `{granted, duplicated, skipped, txn}`：`granted` 为本次实际入账额度，
    重复调用时为 0 且 `duplicated=True`。
    """
    amount = trial_credits() if credits is None else max(0, int(credits))
    if amount <= 0:
        return {"granted": 0, "duplicated": False, "skipped": True, "txn": None}

    if tenant_id in _mem_trial_checked:
        return {"granted": 0, "duplicated": True, "skipped": False, "txn": None}

    key = f"trial:{tenant_id}"
    hit = await _txn_by_idem(key)
    if hit is not None:
        _mem_trial_checked.add(tenant_id)
        return {"granted": 0, "duplicated": True, "skipped": False, "txn": hit}

    try:
        res = await top_up(
            tenant_id, amount,
            operator_id=TRIAL_OPERATOR, idempotency_key=key, memo=TRIAL_MEMO,
        )
    except ValueError:
        # 极少数并发竞态（唯一索引已在别处抢先入账）——不影响业务，视为已发放
        _mem_trial_checked.add(tenant_id)
        return {"granted": 0, "duplicated": True, "skipped": False, "txn": None}

    _mem_trial_checked.add(tenant_id)
    granted = 0 if res["duplicated"] else amount
    _logger.info("租户 %s 体验额度发放：%d（重复=%s）", tenant_id, granted, res["duplicated"])
    return {"granted": granted, "duplicated": res["duplicated"], "skipped": False, "txn": res["txn"]}


async def list_txns(tenant_id: str, limit: int = 200) -> list[dict[str, Any]]:
    session = await repo.db_session()
    if session is not None:
        from sqlalchemy import select

        from app.db.pg_models import BalanceTxnRow

        async with session:
            rows = (await session.execute(
                select(BalanceTxnRow).where(BalanceTxnRow.tenant_id == tenant_id)
                .order_by(BalanceTxnRow.created_at.desc()).limit(limit)
            )).scalars().all()
        return [_txn_dict(r) for r in rows]
    rows = [t for t in _mem_txns if t["tenant_id"] == tenant_id]
    rows.sort(key=lambda t: t["created_at"], reverse=True)
    return [dict(t) for t in rows[:limit]]


# -------------------------------------------------------------- 用量 ----
async def record_usage(
    *,
    tenant_id: str,
    user_id: str = "",
    session_id: str = "",
    message_id: str = "",
    provider: str = "",
    model: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    credits: int = 0,
    degraded: bool = False,
) -> dict[str, Any]:
    row = {
        "id": uuid.uuid4().hex[:16], "tenant_id": tenant_id or "public", "user_id": user_id,
        "session_id": session_id, "message_id": message_id, "provider": provider,
        "model": model, "prompt_tokens": int(prompt_tokens),
        "completion_tokens": int(completion_tokens), "credits": int(credits),
        "degraded": 1 if degraded else 0, "created_at": _now_ms(),
    }
    session = await repo.db_session()
    if session is not None:
        from app.db.pg_models import UsageEventRow

        async with session:
            session.add(UsageEventRow(**row))
            await session.commit()
    else:
        _mem_usage.append(dict(row))
    return row


async def usage_rollup(
    tenant_id: str | None = None, since_ms: int = 0, granularity: str = "day",
) -> list[dict[str, Any]]:
    """按 (时间桶 × 租户 × 供应商/模型) 聚合；tenant_id 为空 = 全平台。

    `granularity`：day（默认，财务对账口径）/ hour（排查用量突刺）。
    分组在 Python 里做：时间桶要用 UTC+8 切，各数据库日期函数方言不同，
    写进 SQL 反而会引入"本地能过、上云错一天"的漂移。
    """
    granularity = granularity if granularity in ("day", "hour") else "day"
    session = await repo.db_session()
    if session is not None:
        from sqlalchemy import select

        from app.db.pg_models import UsageEventRow

        async with session:
            q = select(UsageEventRow)
            if tenant_id:
                q = q.where(UsageEventRow.tenant_id == tenant_id)
            if since_ms:
                q = q.where(UsageEventRow.created_at >= since_ms)
            rows = (await session.execute(q)).scalars().all()
        events = [
            {"tenant_id": r.tenant_id, "provider": r.provider, "model": r.model,
             "prompt_tokens": int(r.prompt_tokens or 0), "completion_tokens": int(r.completion_tokens or 0),
             "credits": int(r.credits or 0), "degraded": int(r.degraded or 0),
             "created_at": int(r.created_at or 0)}
            for r in rows
        ]
    else:
        events = list(_mem_usage)

    buckets: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for e in events:
        if since_ms and e["created_at"] < since_ms:
            continue
        if tenant_id and e.get("tenant_id") != tenant_id:
            continue
        key = (bucket_of(e["created_at"], granularity), e.get("tenant_id") or "public",
               e.get("provider") or "-", e.get("model") or "-")
        # `date` 即"时间桶"：day 粒度是 `2026-09-14`，hour 粒度是 `2026-09-14 19:00`
        b = buckets.setdefault(key, {
            "date": key[0], "tenant_id": key[1], "provider": key[2], "model": key[3],
            "calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "credits": 0, "degraded_calls": 0, "cost_usd": 0.0,
        })
        b["calls"] += 1
        b["prompt_tokens"] += e["prompt_tokens"]
        b["completion_tokens"] += e["completion_tokens"]
        b["credits"] += e["credits"]
        b["degraded_calls"] += 1 if e["degraded"] else 0
        b["cost_usd"] += estimate_cost_usd(e.get("model") or "", e["prompt_tokens"], e["completion_tokens"])

    out = list(buckets.values())
    for b in out:
        b["cost_usd"] = round(b["cost_usd"], 6)
    out.sort(key=lambda b: (b["date"], b["tenant_id"], b["provider"], b["model"]), reverse=True)
    return out


# ---------------------------------------------------------- bad-case ----
async def record_feedback(
    *,
    tenant_id: str,
    session_id: str = "",
    message_id: str = "",
    trace_id: str = "",
    query: str = "",
    retrieved: list[dict[str, Any]] | None = None,
    model: str = "",
    degraded: bool = False,
    error_code: str = "",
    verdict: str = "",
    tags: list[str] | None = None,
    note: str = "",
    source: str = "explicit",
) -> dict[str, Any]:
    """登记一条 bad-case。**写入即脱敏**——库里不留明文凭证与隐私。"""
    row = {
        "id": uuid.uuid4().hex[:16], "tenant_id": tenant_id or "public",
        "session_id": session_id, "message_id": message_id, "trace_id": trace_id,
        "query": redact(query), "retrieved_json": json.dumps(retrieved or [], ensure_ascii=False),
        "model": model, "degraded": 1 if degraded else 0, "error_code": error_code,
        "verdict": verdict, "tags": ",".join(tags or [])[:200],
        "note": redact(note), "source": source, "created_at": _now_ms(),
    }
    session = await repo.db_session()
    if session is not None:
        from app.db.pg_models import FeedbackRow

        async with session:
            session.add(FeedbackRow(**row))
            await session.commit()
    else:
        _mem_feedback.append(dict(row))
    return row


async def list_feedback(
    tenant_id: str | None = None,
    *,
    since_ms: int = 0,
    verdict: str = "",
    limit: int = 500,
) -> list[dict[str, Any]]:
    """查询 bad-case。`tenant_id` 由调用方按权限强制注入，不接受客户端传值。"""
    session = await repo.db_session()
    if session is not None:
        from sqlalchemy import select

        from app.db.pg_models import FeedbackRow

        async with session:
            q = select(FeedbackRow)
            if tenant_id:
                q = q.where(FeedbackRow.tenant_id == tenant_id)
            if since_ms:
                q = q.where(FeedbackRow.created_at >= since_ms)
            if verdict:
                q = q.where(FeedbackRow.verdict == verdict)
            rows = (await session.execute(
                q.order_by(FeedbackRow.created_at.desc()).limit(limit)
            )).scalars().all()
        return [
            {"id": r.id, "tenant_id": r.tenant_id, "session_id": r.session_id,
             "message_id": r.message_id, "trace_id": r.trace_id, "query": r.query,
             "retrieved": json.loads(r.retrieved_json or "[]"), "model": r.model,
             "degraded": bool(r.degraded), "error_code": r.error_code, "verdict": r.verdict,
             "tags": [t for t in (r.tags or "").split(",") if t], "note": r.note,
             "source": r.source, "created_at": int(r.created_at or 0)}
            for r in rows
        ]
    rows = [dict(f) for f in _mem_feedback]
    if tenant_id:
        rows = [f for f in rows if f["tenant_id"] == tenant_id]
    if since_ms:
        rows = [f for f in rows if f["created_at"] >= since_ms]
    if verdict:
        rows = [f for f in rows if f.get("verdict") == verdict]
    rows.sort(key=lambda f: f["created_at"], reverse=True)
    out = []
    for f in rows[:limit]:
        item = dict(f)
        item["retrieved"] = json.loads(f.get("retrieved_json") or "[]")
        item.pop("retrieved_json", None)
        item["tags"] = [t for t in (f.get("tags") or "").split(",") if t]
        out.append(item)
    return out


# ------------------------------------------------------------ 计费上下文 ----
async def tenant_billing_context(tenant_id: str) -> dict[str, Any]:
    """问答链路用的租户计费上下文：是否冻结 / 日上限覆盖 / 档位覆盖。"""
    acc = await get_tenant(tenant_id)
    if acc is None:
        return {"frozen": False, "daily_cap_override": None, "tier_override": None}
    return {
        "frozen": bool(acc.get("frozen")),
        "daily_cap_override": acc.get("daily_cap_override"),
        "tier_override": acc.get("tier_override"),
    }
