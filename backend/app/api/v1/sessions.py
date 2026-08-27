"""会话管理接口：严格 sessionId 隔离，独立保存历史与配置。"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

import app.db.relational as repo

router = APIRouter()


class SessionBody(BaseModel):
    title: str = "新对话"


@router.post("/sessions")
async def create_session(body: SessionBody | None = None):
    return await repo.create_session((body.title if body else None) or "新对话")


@router.get("/sessions")
async def list_sessions():
    return await repo.list_sessions()


@router.get("/sessions/{session_id}/messages")
async def session_messages(session_id: str):
    return await repo.list_messages(session_id)


@router.patch("/sessions/{session_id}")
async def rename_session(session_id: str, body: SessionBody):
    await repo.rename_session(session_id, body.title)
    return {"ok": True}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    await repo.delete_session(session_id)
    return {"ok": True}
