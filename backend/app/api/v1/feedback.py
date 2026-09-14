# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""用户反馈与租户自服务导出（P1-C 第三部分）。

两类来源
--------
- **显式反馈**：前端点踩（thumbs-down）+ 错误标签 + 选填理由 → `POST /feedback`。
- **隐式异常**：LLM 熔断（`LLMUnavailable`）、生成异常、检索零召回 →
  由 `chat_stream` 在服务端直接登记（`source=implicit`），不需要用户操作。

隔离口径（重点）
----------------
租户管理员**只能导出自己租户**的 bad-case，且租户值**一律由服务端从用户记录
解析**（`resolve_tenant`）——接口不接受任何 `tenant_id` 参数，因为那是可以伪造的
越权口子。平台超管的跨租户排查走 `/admin/ops/badcases`，鉴权是另一套
（管理口令换的 admin JWT），两者互不越界。
"""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.v1.auth import AuthUser, current_user_optional
from app.core.tenancy import resolve_tenant
from app.services.notebook import store as notebook_store
from app.services.ops import store

router = APIRouter()

_FEEDBACK_COLUMNS = (
    "created_at", "tenant_id", "trace_id", "message_id", "session_id", "source",
    "verdict", "error_code", "tags", "model", "degraded", "query", "retrieved", "note",
)


class FeedbackBody(BaseModel):
    session_id: str = ""
    message_id: str = ""
    trace_id: str = ""          # 缺省回落 message_id：一次问答一个 trace
    verdict: str = ""           # up | down
    tags: list[str] = []        # 答案不准 / 答非所问 / 代码运行报错 …
    note: str = ""
    query: str = ""             # 便于离线复盘；服务端会脱敏后落库


@router.post("/feedback")
async def submit_feedback(body: FeedbackBody, user: AuthUser = Depends(current_user_optional)):
    """登记一条显式反馈。租户归属由服务端解析，客户端说了不算。"""
    verdict = (body.verdict or "").strip().lower()
    if verdict not in ("up", "down"):
        raise HTTPException(status_code=400, detail="verdict 只能是 up 或 down")
    row = await store.record_feedback(
        tenant_id=resolve_tenant(user),
        session_id=body.session_id,
        message_id=body.message_id,
        trace_id=body.trace_id or body.message_id,
        query=body.query,
        verdict=verdict,
        tags=body.tags[:10],
        note=body.note[:2000],
        source="explicit",
    )
    return {"ok": True, "id": row["id"], "tenant_id": row["tenant_id"]}


@router.get("/ops/badcases")
async def export_badcases(
    days: int = 7,
    verdict: str = "",
    format: str = "json",
    user: AuthUser = Depends(current_user_optional),
):
    """租户管理员导出**本租户**的 bad-case（CSV / JSON）。

    权限：登录用户且 `role = tenant_admin`。平台超管不在此授权——
    它有自己的跨租户接口，避免"一个接口两种权限语义"。
    """
    if user.anonymous:
        raise HTTPException(status_code=401, detail="请先登录")
    if (user.role or "member") != "tenant_admin":
        raise HTTPException(status_code=403, detail="仅租户管理员可导出本租户 bad-case")

    days = max(1, min(90, days))
    since_ms = int((time.time() - days * 86400) * 1000)
    tenant = resolve_tenant(user)                      # 强制作用域，不接受客户端传值
    rows = await store.list_feedback(tenant, since_ms=since_ms, verdict=verdict)

    if format.lower() == "csv":
        flat = [
            {**r, "tags": ",".join(r.get("tags") or []),
             "retrieved": json.dumps(r.get("retrieved") or [], ensure_ascii=False),
             "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r.get("created_at", 0) / 1000))
             if r.get("created_at") else ""}
            for r in rows
        ]
        from fastapi.responses import PlainTextResponse

        import csv
        import io

        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=list(_FEEDBACK_COLUMNS), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(flat)
        return PlainTextResponse(
            content="﻿" + buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=badcases.csv"},
        )
    return {"tenant_id": tenant, "window_days": days, "count": len(rows), "items": rows}


_STRUGGLE_COLUMNS = (
    "concept_tag", "mistakes", "passive_converge", "converge_fail_rate",
    "avg_mastery", "mastered", "active", "last_at",
)


@router.get("/ops/concepts/struggles")
async def concept_struggles(
    limit: int = 50,
    format: str = "json",
    user: AuthUser = Depends(current_user_optional),
):
    """租户级高频卡点聚合：按知识点统计错题数 / 平均掌握度 / 被动挂科率。

    权限与 `/ops/badcases` 完全一致（登录 + `role=tenant_admin`），
    且 `tenant_id` **只来自服务端解析**——教研看不到别的机构的盲区分布。
    数据源是同租户学生的错题本（`mistake_notebook`），不跨租户。
    """
    if user.anonymous:
        raise HTTPException(status_code=401, detail="请先登录")
    if (user.role or "member") != "tenant_admin":
        raise HTTPException(status_code=403, detail="仅租户管理员可查看本租户卡点分布")

    tenant = resolve_tenant(user)
    limit = max(1, min(200, limit))
    rows = await notebook_store.tenant_struggles(tenant, limit=limit)

    if format.lower() == "csv":
        import csv
        import io

        from fastapi.responses import PlainTextResponse

        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=list(_STRUGGLE_COLUMNS), extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            flat = dict(r)
            flat["last_at"] = (
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["last_at"] / 1000))
                if r.get("last_at") else ""
            )
            writer.writerow(flat)
        return PlainTextResponse(
            content="﻿" + buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=concept_struggles.csv"},
        )
    return {"tenant_id": tenant, "count": len(rows), "items": rows}
