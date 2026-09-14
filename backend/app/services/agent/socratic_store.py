# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""苏格拉底 FSM 状态的持久化层。

为什么单开一层，而不是继续用 `relational.get_tutor_state`
--------------------------------------------------------
旧的 `tutor_state` 是**进程内字典**：单机演示够用，但一旦多副本，
学生的引导阶段会随负载均衡在副本间来回跳（副本 A 记到 GUIDING，
下一个请求打到副本 B 又变成 DIAGNOSING）。阶段是"跨轮次续接"的状态，
必须落到共享存储，这与限流器/账本外置到 Redis 是同一个道理。

回落策略与 `services/ops/store.py` 保持一致：DB 不可用时用进程内字典兜底，
单副本演示可用；多副本需配 POSTGRES_DSN。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import app.db.relational as repo
from app.services.agent.socratic_fsm import SocraticState

_logger = logging.getLogger("app.services.agent.socratic_store")

# 进程内兜底（DB 不可用时）
_mem: dict[str, dict[str, Any]] = {}

# FSM 侧字段（与 SocraticState.to_dict 的键一致）
_STATE_FIELDS = (
    "phase", "hint_level", "turns_in_phase", "stuck_count", "escape_attempts",
    "question_count", "converge_failed", "guard_blocked", "last_signal",
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def reset_memory() -> None:
    """测试用：清空进程内兜底数据。"""
    _mem.clear()


def _row_to_state(r: Any) -> dict[str, Any]:
    return {
        "phase": r.phase,
        "hint_level": int(r.hint_level or 0),
        "turns_in_phase": int(r.turns_in_phase or 0),
        "stuck_count": int(r.stuck_count or 0),
        "escape_attempts": int(r.escape_attempts or 0),
        "question_count": int(r.question_count or 0),
        "converge_failed": bool(r.converge_failed),
        "guard_blocked": bool(r.guard_blocked),
        "last_signal": r.last_signal or "none",
        "history": json.loads(r.history_json or "[]"),
    }


async def load_state(session_id: str) -> dict[str, Any] | None:
    """读取会话的 FSM 状态；不存在返回 None（调用方按初始态起）。"""
    if not session_id:
        return None
    session = await repo.db_session()
    if session is not None:
        from app.db.pg_models import SocraticSessionRow

        async with session:
            row = await session.get(SocraticSessionRow, session_id)
        return _row_to_state(row) if row else None
    stored = _mem.get(session_id)
    if stored is None or "phase" not in stored:
        # 可能只有 pending_quiz（尚未流转过状态机）——按"无状态"处理
        return None
    return {
        "phase": stored["phase"], "hint_level": stored["hint_level"],
        "turns_in_phase": stored["turns_in_phase"], "stuck_count": stored["stuck_count"],
        "escape_attempts": stored["escape_attempts"], "question_count": stored["question_count"],
        "converge_failed": stored["converge_failed"], "guard_blocked": stored["guard_blocked"],
        "last_signal": stored["last_signal"], "history": list(stored["history"]),
    }


async def save_state(
    session_id: str,
    state: SocraticState,
    *,
    tenant_id: str = "public",
    user_id: str = "",
    course_id: str = "",
    concept_tag: str = "",
) -> None:
    """Upsert 会话 FSM 状态。

    写入失败**只记日志不抛**：状态落库失败固然会丢一次续接能力，
    但绝不该因此让整轮问答失败——与运营台账同一条工程红线。
    """
    if not session_id:
        return
    fsm = state.to_dict()
    history_json = json.dumps(fsm.get("history") or [], ensure_ascii=False)
    now = _now_ms()

    session = await repo.db_session()
    if session is not None:
        from app.db.pg_models import SocraticSessionRow

        try:
            async with session:
                row = await session.get(SocraticSessionRow, session_id)
                if row is None:
                    row = SocraticSessionRow(
                        session_id=session_id, tenant_id=tenant_id or "public",
                        user_id=user_id or "", course_id=course_id or "",
                        concept_tag=concept_tag or "", created_at=now,
                    )
                    session.add(row)
                # 元数据只在有值时刷新，避免后续轮次把首轮记录覆盖成空
                if tenant_id:
                    row.tenant_id = tenant_id
                if user_id:
                    row.user_id = user_id
                if course_id:
                    row.course_id = course_id
                if concept_tag:
                    row.concept_tag = concept_tag
                for key in _STATE_FIELDS:
                    value = fsm.get(key)
                    if key in ("converge_failed", "guard_blocked"):
                        value = 1 if value else 0
                    setattr(row, key, value)
                row.history_json = history_json
                row.updated_at = now
                await session.commit()
            return
        except Exception as exc:  # noqa: BLE001 - 状态落库失败不得拖垮问答
            _logger.warning("苏格拉底状态持久化失败（不影响本轮引导）: %s", exc)
            return

    existing = _mem.get(session_id)
    if existing is None:
        _mem[session_id] = {
            "tenant_id": tenant_id or "public", "user_id": user_id or "",
            "course_id": course_id or "", "concept_tag": concept_tag or "",
            "created_at": now, **{k: fsm.get(k) for k in _STATE_FIELDS}, "history": list(fsm.get("history") or []),
        }
    else:
        if tenant_id:
            existing["tenant_id"] = tenant_id
        if user_id:
            existing["user_id"] = user_id
        if course_id:
            existing["course_id"] = course_id
        if concept_tag:
            existing["concept_tag"] = concept_tag
        for key in _STATE_FIELDS:
            existing[key] = fsm.get(key)
        existing["history"] = list(fsm.get("history") or [])
    _mem[session_id]["updated_at"] = now


# ------------------------------------------------------- 挂起自测题 ----
# CONVERGING 出题后，题目要"挂起"等待学生作答；下一轮收到作答才能判分。
# 这一步必须落库：多副本下若只放在内存，学生下一轮被负载均衡到别的副本，
# 就会"找不到题 → 判分不了 → 状态机永远卡在 CONVERGING"。
async def set_pending_quiz(session_id: str, quiz: dict | None) -> None:
    """挂起 / 清除当前待作答的自测题（`None` = 清除）。"""
    if not session_id:
        return
    payload = json.dumps(quiz, ensure_ascii=False) if quiz else ""
    now = _now_ms()
    session = await repo.db_session()
    if session is not None:
        from app.db.pg_models import SocraticSessionRow

        try:
            async with session:
                row = await session.get(SocraticSessionRow, session_id)
                if row is None:
                    row = SocraticSessionRow(session_id=session_id, created_at=now)
                    session.add(row)
                row.pending_quiz_json = payload
                row.updated_at = now
                await session.commit()
            return
        except Exception as exc:  # noqa: BLE001 - 挂起题写入失败不得拖垮本轮回合
            _logger.warning("挂起自测题写入失败: %s", exc)
            return
    entry = _mem.setdefault(session_id, {})
    entry["pending_quiz"] = quiz
    entry["updated_at"] = now


async def get_pending_quiz(session_id: str) -> dict | None:
    """读取挂起的自测题；没有则返回 None。"""
    if not session_id:
        return None
    session = await repo.db_session()
    if session is not None:
        from app.db.pg_models import SocraticSessionRow

        async with session:
            row = await session.get(SocraticSessionRow, session_id)
        raw = (getattr(row, "pending_quiz_json", "") or "") if row else ""
        if not raw.strip():
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None
    quiz = (_mem.get(session_id) or {}).get("pending_quiz")
    return quiz if isinstance(quiz, dict) else None


async def delete_state(session_id: str) -> None:
    """删除会话状态（会话被删除时清理；缺行视为成功）。"""
    if not session_id:
        return
    session = await repo.db_session()
    if session is not None:
        from sqlalchemy import delete

        from app.db.pg_models import SocraticSessionRow

        async with session:
            await session.execute(delete(SocraticSessionRow).where(SocraticSessionRow.session_id == session_id))
            await session.commit()
        return
    _mem.pop(session_id, None)


async def list_states(tenant_id: str | None = None, *, limit: int = 500) -> list[dict[str, Any]]:
    """按租户列出引导会话状态（教研侧观察卡点分布的数据源）。"""
    session = await repo.db_session()
    if session is not None:
        from sqlalchemy import select

        from app.db.pg_models import SocraticSessionRow

        async with session:
            q = select(SocraticSessionRow)
            if tenant_id:
                q = q.where(SocraticSessionRow.tenant_id == tenant_id)
            rows = (await session.execute(
                q.order_by(SocraticSessionRow.updated_at.desc()).limit(limit)
            )).scalars().all()
        return [
            {"session_id": r.session_id, "tenant_id": r.tenant_id, "user_id": r.user_id,
             "course_id": r.course_id, "concept_tag": r.concept_tag, **_row_to_state(r)}
            for r in rows
        ]
    rows = [dict(v, session_id=k) for k, v in _mem.items()]
    if tenant_id:
        rows = [r for r in rows if r.get("tenant_id") == tenant_id]
    rows.sort(key=lambda r: r.get("updated_at", 0), reverse=True)
    return rows[:limit]
