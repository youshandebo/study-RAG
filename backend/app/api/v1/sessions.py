# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""会话管理接口：会员 owner 强制隔离，独立保存历史与配置。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import app.db.relational as repo
from app.api.v1.auth import AuthUser, current_user_optional

router = APIRouter()


class SessionBody(BaseModel):
    title: str = "新对话"


class SessionMetaBody(BaseModel):
    subject: str | None = None
    course_id: str | None = None
    chapter: str | None = None
    retrieval_mode: str | None = None
    time_alpha_override: float | None = None


def _owner_id(user: AuthUser) -> str | None:
    return None if user.anonymous else user.id


async def _assert_session_owner(session_id: str, user: AuthUser) -> None:
    """强制会话隔离：注册用户只能访问自己的会话（匿名演示模式不校验）。"""
    if user.anonymous:
        return
    owner = await repo.get_session_owner(session_id)
    if owner is None:
        return  # 升级前的存量会话无归属：保持可读，避免历史数据被锁死
    if owner != user.id:
        raise HTTPException(status_code=403, detail="无权访问该会话")


@router.post("/sessions")
async def create_session(body: SessionBody | None = None, user: AuthUser = Depends(current_user_optional)):
    return await repo.create_session((body.title if body else None) or "新对话", owner=_owner_id(user))


@router.get("/sessions")
async def list_sessions(user: AuthUser = Depends(current_user_optional)):
    return await repo.list_sessions(owner=_owner_id(user), require_owner=not user.anonymous)


@router.get("/sessions/{session_id}/messages")
async def session_messages(session_id: str, user: AuthUser = Depends(current_user_optional)):
    await _assert_session_owner(session_id, user)
    return await repo.list_messages(session_id)


@router.get("/sessions/{session_id}/usage")
async def session_usage(session_id: str, user: AuthUser = Depends(current_user_optional)):
    """按会话聚合 Token 用量：累计输入/输出、轮数与最近一轮的上下文容量明细。"""
    await _assert_session_owner(session_id, user)
    msgs = await repo.list_messages(session_id)
    turns = 0
    input_total = output_total = total = 0
    latest_ctx = None
    for m in msgs:
        usage = m.get("usage")
        if not isinstance(usage, dict):
            continue
        turns += 1
        input_total += int(usage.get("input", 0))
        output_total += int(usage.get("output", 0))
        total += int(usage.get("total", 0))
        if usage.get("context_breakdown"):
            latest_ctx = usage
    return {
        "turns": turns,
        "input_total": input_total,
        "output_total": output_total,
        "total": total,
        "latest_context": latest_ctx,
    }


@router.patch("/sessions/{session_id}")
async def rename_session(session_id: str, body: SessionBody, user: AuthUser = Depends(current_user_optional)):
    await _assert_session_owner(session_id, user)
    await repo.rename_session(session_id, body.title)
    return {"ok": True}


@router.patch("/sessions/{session_id}/meta")
async def patch_session_meta(session_id: str, body: SessionMetaBody, user: AuthUser = Depends(current_user_optional)):
    """课程绑定 / 检索模式持久化（Postgres 配置时落库）。"""
    await _assert_session_owner(session_id, user)
    await repo.update_session_meta(session_id, body.model_dump(exclude_none=False))
    return {"ok": True}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, user: AuthUser = Depends(current_user_optional)):
    await _assert_session_owner(session_id, user)
    await repo.delete_session(session_id)
    return {"ok": True}
