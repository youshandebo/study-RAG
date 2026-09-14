# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""错题本数据层：入库 / 到期待复习 / 复习结算 / 租户卡点聚合。

三条不可妥协的口径
------------------
1. **双重隔离**：租户是硬边界，`user_id` 是第二道边界。查错题必须同时带
   `tenant_id` 与 `user_id`，且两者**都由服务端解析**（来自 JWT + 用户记录），
   接口绝不接受客户端传入——错题是比知识库更私密的个人数据。
2. **判分与调度都在服务端**：`apply_review` 收的是"学生作答"，
   判分（确定性规则 / 结构化）与 Leitner 升降盒都在这里完成，
   客户端说了不算，否则改个请求体就能把 Box 刷到 5。
3. **判分不确定 ≠ 判错**：`correct is None` 时只记复习次数与时间，
   **不动盒子、不扣掌握度**——技术性"判不出来"不能变成对学生的惩罚。

DB 不可用时回落进程内字典（单副本演示可用；多副本需配 POSTGRES_DSN）。
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

import app.db.relational as repo
from app.services.notebook import leitner

_logger = logging.getLogger("app.services.notebook.store")

SOURCE_PASSIVE = "passive_converge"
SOURCE_MANUAL = "active_manual"
SOURCE_EXAM = "exam_failed"
_VALID_SOURCES = (SOURCE_PASSIVE, SOURCE_MANUAL, SOURCE_EXAM)

# 进程内兜底（DB 不可用时）
_mem: dict[str, dict[str, Any]] = {}


def _now_ms() -> int:
    return int(time.time() * 1000)


def reset_memory() -> None:
    """测试用：清空进程内兜底数据。"""
    _mem.clear()


def _row_dict(r: Any) -> dict[str, Any]:
    return {
        "id": r.id,
        "tenant_id": r.tenant_id,
        "user_id": r.user_id,
        "course_id": r.course_id or "",
        "concept_tag": r.concept_tag or "",
        "source_type": r.source_type or SOURCE_PASSIVE,
        "session_id": r.session_id or "",
        "trace_id": r.trace_id or "",
        "question_context": r.question_context or "",
        "reference_answer": r.reference_answer or "",
        "options": json.loads(r.options_json or "[]") or None,
        "misconception": r.misconception or "",
        "leitner_box": int(r.leitner_box or 1),
        "mastery_score": int(r.mastery_score or 0),
        "review_count": int(r.review_count or 0),
        "last_reviewed_at": int(r.last_reviewed_at or 0),
        "next_review_at": int(r.next_review_at or 0),
        "status": r.status or leitner.STATUS_ACTIVE,
        "created_at": int(r.created_at or 0),
        "updated_at": int(r.updated_at or 0),
    }


def _mem_dict(v: dict[str, Any]) -> dict[str, Any]:
    item = dict(v)
    item["options"] = json.loads(v.get("options_json") or "[]") or None
    item.pop("options_json", None)
    return item


# ------------------------------------------------------------ 入库 ----
async def add_mistake(
    *,
    tenant_id: str,
    user_id: str = "",
    course_id: str = "",
    concept_tag: str = "",
    source_type: str = SOURCE_PASSIVE,
    session_id: str = "",
    trace_id: str = "",
    question_context: str = "",
    reference_answer: str = "",
    options: list[str] | None = None,
    misconception: str = "",
    fail_count: int = 1,
) -> dict[str, Any]:
    """新增一条错题；Leitner 元数据由 `leitner.new_entry` 统一初始化。"""
    now = _now_ms()
    meta = leitner.new_entry(fail_count=fail_count, now_ms=now)
    row = {
        "id": uuid.uuid4().hex[:16],
        "tenant_id": tenant_id or "public",
        "user_id": user_id,
        "course_id": course_id,
        "concept_tag": concept_tag[:200],
        "source_type": source_type if source_type in _VALID_SOURCES else SOURCE_PASSIVE,
        "session_id": session_id,
        "trace_id": trace_id,
        "question_context": question_context,
        "reference_answer": reference_answer,
        "options_json": json.dumps(options or [], ensure_ascii=False),
        "misconception": misconception,
        **meta,
        "created_at": now,
        "updated_at": now,
    }

    session = await repo.db_session()
    if session is not None:
        from app.db.pg_models import MistakeNotebookRow

        async with session:
            session.add(MistakeNotebookRow(**row))
            await session.commit()
        result = dict(row)
        result["options"] = options or None
        result.pop("options_json", None)
        return result

    _mem[row["id"]] = dict(row)
    return _mem_dict(row)


# ------------------------------------------------------------ 查询 ----
async def get(tenant_id: str, mistake_id: str) -> dict[str, Any] | None:
    """按 (租户, id) 取一条——**tenant 参与查询条件**，杜绝跨租户读取。"""
    session = await repo.db_session()
    if session is not None:
        from sqlalchemy import select

        from app.db.pg_models import MistakeNotebookRow

        async with session:
            row = (await session.execute(
                select(MistakeNotebookRow).where(
                    MistakeNotebookRow.id == mistake_id,
                    MistakeNotebookRow.tenant_id == tenant_id,
                )
            )).scalar_one_or_none()
        return _row_dict(row) if row else None
    v = _mem.get(mistake_id)
    if v is None or v["tenant_id"] != tenant_id:
        return None
    return _mem_dict(v)


async def list_due(
    tenant_id: str, user_id: str, *, now_ms: int | None = None, limit: int = 100,
) -> list[dict[str, Any]]:
    """到期（含逾期）待复习的错题：`status=active` 且 `next_review_at <= now`。"""
    now = _now_ms() if now_ms is None else int(now_ms)
    session = await repo.db_session()
    if session is not None:
        from sqlalchemy import select

        from app.db.pg_models import MistakeNotebookRow

        async with session:
            rows = (await session.execute(
                select(MistakeNotebookRow).where(
                    MistakeNotebookRow.tenant_id == tenant_id,
                    MistakeNotebookRow.user_id == user_id,
                    MistakeNotebookRow.status == leitner.STATUS_ACTIVE,
                    MistakeNotebookRow.next_review_at <= now,
                ).order_by(MistakeNotebookRow.next_review_at.asc()).limit(limit)
            )).scalars().all()
        return [_row_dict(r) for r in rows]

    out = [
        _mem_dict(v) for v in _mem.values()
        if v["tenant_id"] == tenant_id and v["user_id"] == user_id
        and v["status"] == leitner.STATUS_ACTIVE and int(v["next_review_at"]) <= now
    ]
    out.sort(key=lambda x: x["next_review_at"])
    return out[:limit]


async def list_for_user(
    tenant_id: str, user_id: str, *, status: str = "", limit: int = 200,
) -> list[dict[str, Any]]:
    """列出某学生的错题本（可按状态过滤）。"""
    session = await repo.db_session()
    if session is not None:
        from sqlalchemy import select

        from app.db.pg_models import MistakeNotebookRow

        async with session:
            q = select(MistakeNotebookRow).where(
                MistakeNotebookRow.tenant_id == tenant_id,
                MistakeNotebookRow.user_id == user_id,
            )
            if status:
                q = q.where(MistakeNotebookRow.status == status)
            rows = (await session.execute(
                q.order_by(MistakeNotebookRow.updated_at.desc()).limit(limit)
            )).scalars().all()
        return [_row_dict(r) for r in rows]

    out = [
        _mem_dict(v) for v in _mem.values()
        if v["tenant_id"] == tenant_id and v["user_id"] == user_id
        and (not status or v["status"] == status)
    ]
    out.sort(key=lambda x: x["updated_at"], reverse=True)
    return out[:limit]


# ------------------------------------------------------------ 结算 ----
async def apply_review(
    tenant_id: str, mistake_id: str, *, correct: bool | None, now_ms: int | None = None,
) -> dict[str, Any] | None:
    """按判分结果结算一次复习，返回更新后的条目；不存在/越权返回 None。

    - `correct=True`：升一盒 + 加掌握度 + 按新盒推迟
    - `correct=False`：打回 Box 1 + 扣掌握度 + 次日重考
    - `correct=None`：只记"复习过一次"，**不动盒子与掌握度**
    """
    now = _now_ms() if now_ms is None else int(now_ms)
    current = await get(tenant_id, mistake_id)
    if current is None:
        return None

    review_count = int(current.get("review_count") or 0) + 1
    patch: dict[str, Any] = {"review_count": review_count, "last_reviewed_at": now, "updated_at": now}
    if correct is True:
        patch.update(leitner.on_correct(current["leitner_box"], current["mastery_score"], now))
    elif correct is False:
        patch.update(leitner.on_wrong(current["leitner_box"], current["mastery_score"], now))
    # correct is None：只记录复习行为，不改变调度（见模块头第 3 条）

    session = await repo.db_session()
    if session is not None:
        from sqlalchemy import update

        from app.db.pg_models import MistakeNotebookRow

        async with session:
            await session.execute(
                update(MistakeNotebookRow).where(
                    MistakeNotebookRow.id == mistake_id,
                    MistakeNotebookRow.tenant_id == tenant_id,
                ).values(**patch)
            )
            await session.commit()
        return await get(tenant_id, mistake_id)

    v = _mem.get(mistake_id)
    if v is None or v["tenant_id"] != tenant_id:
        return None
    v.update(patch)
    return _mem_dict(v)


async def archive(tenant_id: str, mistake_id: str) -> bool:
    return await _set_status(tenant_id, mistake_id, leitner.STATUS_ARCHIVED)


async def _set_status(tenant_id: str, mistake_id: str, status: str) -> bool:
    session = await repo.db_session()
    if session is not None:
        from sqlalchemy import update

        from app.db.pg_models import MistakeNotebookRow

        async with session:
            result = await session.execute(
                update(MistakeNotebookRow).where(
                    MistakeNotebookRow.id == mistake_id,
                    MistakeNotebookRow.tenant_id == tenant_id,
                ).values(status=status, updated_at=_now_ms())
            )
            await session.commit()
        return bool(result.rowcount)
    v = _mem.get(mistake_id)
    if v is None or v["tenant_id"] != tenant_id:
        return False
    v["status"] = status
    v["updated_at"] = _now_ms()
    return True


# ------------------------------------------------------- 租户卡点聚合 ----
async def tenant_struggles(tenant_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """按 `concept_tag` 聚合本租户学生的共性盲区。

    只统计**本租户**（调用方按权限强制注入 tenant，不接受客户端传值）。
    分组在 Python 里做：与 FinOps 聚合同一取舍——各数据库字符串函数方言不同，
    写进 SQL 会引入"本地能过、上云结果不同"的漂移。
    """
    session = await repo.db_session()
    if session is not None:
        from sqlalchemy import select

        from app.db.pg_models import MistakeNotebookRow

        async with session:
            rows = (await session.execute(
                select(MistakeNotebookRow).where(MistakeNotebookRow.tenant_id == tenant_id)
            )).scalars().all()
        items = [_row_dict(r) for r in rows]
    else:
        items = [_mem_dict(v) for v in _mem.values() if v["tenant_id"] == tenant_id]

    buckets: dict[str, dict[str, Any]] = {}
    for it in items:
        tag = it.get("concept_tag") or "未标注"
        b = buckets.setdefault(tag, {
            "concept_tag": tag, "mistakes": 0, "passive_converge": 0,
            "mastered": 0, "active": 0, "mastery_sum": 0, "last_at": 0,
        })
        b["mistakes"] += 1
        b["mastery_sum"] += int(it.get("mastery_score") or 0)
        if it.get("source_type") == SOURCE_PASSIVE:
            b["passive_converge"] += 1
        if it.get("status") == leitner.STATUS_MASTERED:
            b["mastered"] += 1
        elif it.get("status") == leitner.STATUS_ACTIVE:
            b["active"] += 1
        b["last_at"] = max(b["last_at"], int(it.get("created_at") or 0))

    out = []
    for b in buckets.values():
        total = b["mistakes"] or 1
        out.append({
            "concept_tag": b["concept_tag"],
            "mistakes": b["mistakes"],
            "passive_converge": b["passive_converge"],
            # 被动挂科率：引导自测失败占该知识点错题的比例
            "converge_fail_rate": round(b["passive_converge"] / total, 4),
            "avg_mastery": round(b["mastery_sum"] / total, 1),
            "mastered": b["mastered"],
            "active": b["active"],
            "last_at": b["last_at"],
        })
    # 卡点排序：错题多 → 被动挂科多 → 掌握度低
    out.sort(key=lambda x: (x["mistakes"], x["passive_converge"], -x["avg_mastery"]), reverse=True)
    return out[:limit]
