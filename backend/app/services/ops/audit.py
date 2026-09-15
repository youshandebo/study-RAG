# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""审计日志：记录敏感操作的**主体**（谁做的），而不只是业务结果。

与既有三张表的分工
------------------
- `balance_txns`：钱怎么变的（结果）
- `usage_events`：资源怎么消耗的（结果）
- `feedback_events`：系统哪里答错了（结果）
- `audit_logs`（本模块）：**谁**在什么时候把这些改成了这样（责任）

缺了最后一条，"某切片被设为定版""某租户余额被调减"这类事情只能看到结果、
看不到责任人，机构尽调时答不上"谁有权改、改过几次"。

三条硬约束
----------
1. **只追加**：本模块只提供 `record()` 写与 `list_events()` 读，没有 update/delete。
   不提供修改能力本身就是设计要求——能被改的日志在审计场景里等于不存在。
2. **写时脱敏**：复用 `ops.redact`，密钥/token/手机号在**落库前**就替换掉。
   事后脱敏等于"库里仍留着明文"，二次泄露面已经形成。
3. **绝不反向击垮主服务**：写日志失败只记 WARNING。审计很重要，但它是旁路：
   "审计写不进去就把用户的定版操作回滚"是比丢日志更糟的选择。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

import app.db.relational as repo
from app.services.ops.redact import redact

_logger = logging.getLogger("app.services.ops.audit")

# 进程内兜底（DB 不可用时）：单副本演示仍能看到"刚刚谁改了什么"
_mem_logs: list[dict[str, Any]] = []

# 单条 detail 的体积上限：审计是摘要不是数据仓库，
# 防止有人把整个请求体（含大段课件文本）塞进来把表撑爆。
_MAX_DETAIL_CHARS = 2000


def _now_ms() -> int:
    return int(time.time() * 1000)


def fingerprint(secret: str) -> str:
    """凭据指纹：用于把"同一会话的多次操作"关联起来，而**不落库凭据本身**。

    审计表若记录 admin token 原文，等于把凭据复制到了一个更容易被读的地方
    （审计查询开放给更多人）——那是把审计变成新的泄露面。

    对非字符串入参直接返回空串：审计是旁路，绝不能因为取指纹失败
    就把业务端点打成 500（直接调用路由函数的测试会把 `Depends` 占位对象传进来）。
    """
    if not secret or not isinstance(secret, str):
        return ""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]


def ip_of(request: Any) -> str:
    """容错地取客户端 IP。

    刻意不假设入参一定是 `Request`：审计是旁路，**绝不能因为取一个 IP 失败
    就把业务端点打成 500**（直接调用路由函数的测试、非标准 ASGI 挂载都会
    出现形态不一致）。取不到就留空——审计少一个字段，远好过请求失败。
    """
    try:
        client = getattr(request, "client", None)
        return getattr(client, "host", "") or ""
    except Exception:  # noqa: BLE001 - 任何异常都不该影响主流程
        return ""


def _slim(detail: dict[str, Any] | None) -> str:
    """序列化 + 脱敏 + 截断。任何异常都退化为空串，绝不让日志构造失败影响主流程。"""
    if not detail:
        return ""
    try:
        text = json.dumps(detail, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001 - 不可序列化对象（如 ORM 实例）不应阻断审计
        text = json.dumps({"raw": str(detail)[:_MAX_DETAIL_CHARS]}, ensure_ascii=False)
    text = redact(text)
    return text[:_MAX_DETAIL_CHARS]


async def record(
    action: str,
    *,
    actor: str = "",
    actor_role: str = "",
    tenant_id: str = "",
    target: str = "",
    ip: str = "",
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """追加一条审计。**不抛异常**——调用方无需 try/except 包裹。"""
    row = {
        "ts": _now_ms(),
        "action": (action or "")[:64],
        "actor": (actor or "")[:128],
        "actor_role": (actor_role or "")[:20],
        "tenant_id": (tenant_id or "public")[:64],
        "target": (target or "")[:255],
        "ip": (ip or "")[:64],
        "detail_json": _slim(detail),
    }
    try:
        session = await repo.db_session()
        if session is not None:
            from app.db.pg_models import AuditLogRow

            async with session:
                session.add(AuditLogRow(**row))
                await session.commit()
            row["id"] = None  # 自增主键由 DB 分配
        else:
            _mem_logs.append(dict(row))
    except Exception as exc:  # noqa: BLE001 - 审计失败不得影响业务
        _logger.warning("审计日志写入失败（已忽略，不影响业务）: %s", exc)
    return row


async def record_admin(
    action: str,
    *,
    token: str,
    ip: str = "",
    tenant_id: str = "",
    target: str = "",
    detail: dict[str, Any] | None = None,
) -> None:
    """平台超管操作的统一入口（admin.py 与 admin_ops.py 共用）。

    `token` 只用于生成会话指纹，**不落库**——审计表若存凭据原文，
    等于把凭据复制到一个查询面更宽的表中，审计本身就成了新的泄露面。
    """
    payload = dict(detail or {})
    fp = fingerprint(token)
    if fp:
        payload.setdefault("session", fp)
    await record(
        action,
        actor="platform_admin",
        actor_role="platform_admin",
        tenant_id=tenant_id,
        target=target,
        ip=ip,
        detail=payload,
    )


async def list_events(
    *,
    tenant_id: str = "",
    action: str = "",
    actor: str = "",
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """只读查询。平台超管可不传 tenant_id 看全量；其余调用方一律强制带自己的租户。"""
    limit = max(1, min(1000, int(limit)))
    offset = max(0, int(offset))

    session = await repo.db_session()
    if session is not None:
        from sqlalchemy import select

        from app.db.pg_models import AuditLogRow

        stmt = select(AuditLogRow)
        if tenant_id:
            stmt = stmt.where(AuditLogRow.tenant_id == tenant_id)
        if action:
            stmt = stmt.where(AuditLogRow.action == action)
        if actor:
            stmt = stmt.where(AuditLogRow.actor == actor)
        stmt = stmt.order_by(AuditLogRow.ts.desc(), AuditLogRow.id.desc()).limit(limit).offset(offset)
        async with session:
            rows = (await session.execute(stmt)).scalars().all()
        return [
            {
                "id": r.id, "ts": r.ts, "action": r.action, "actor": r.actor,
                "actor_role": r.actor_role, "tenant_id": r.tenant_id,
                "target": r.target, "ip": r.ip, "detail": r.detail_json,
            }
            for r in rows
        ]

    rows = list(_mem_logs)
    if tenant_id:
        rows = [r for r in rows if r["tenant_id"] == tenant_id]
    if action:
        rows = [r for r in rows if r["action"] == action]
    if actor:
        rows = [r for r in rows if r["actor"] == actor]
    # 次键取插入序：同一毫秒内的多条（批量操作很常见）若只按 ts 排，
    # 顺序会退化成插入序，而审计必须**确定地**倒序给出"最近发生了什么"。
    # 这与 DB 路径的 `id desc` 次键口径一致。
    order = sorted(enumerate(rows), key=lambda pair: (pair[1]["ts"], pair[0]), reverse=True)
    ordered = [row for _idx, row in order]
    return [
        {
            "id": None, "ts": r["ts"], "action": r["action"], "actor": r["actor"],
            "actor_role": r["actor_role"], "tenant_id": r["tenant_id"],
            "target": r["target"], "ip": r["ip"], "detail": r["detail_json"],
        }
        for r in ordered[offset : offset + limit]
    ]


def reset_memory() -> None:
    """测试用：清空进程内兜底。"""
    _mem_logs.clear()
