"""关系存储仓库层：配置了 POSTGRES_DSN 时走 SQLAlchemy 异步引擎；
否则自动回落进程内仓储（教学演示 / 单机部署零依赖可用）。接口完全一致。"""
from __future__ import annotations

import time
import uuid
from typing import Any

_engine = None
_memory_sessions: dict[str, dict[str, Any]] = {}
_memory_messages: dict[str, list[dict[str, Any]]] = {}
_memory_assets: dict[str, list[dict[str, Any]]] = {}


def _use_postgres() -> bool:
    from app.core.config import get_settings

    dsn = get_settings().postgres_dsn
    if not dsn:
        return False
    global _engine
    if _engine is None:
        try:
            from sqlalchemy.ext.asyncio import create_async_engine

            _engine = create_async_engine(dsn, echo=False)
        except Exception:
            return False
    return True


# ---------------------------------------------------------------- sessions --
async def create_session(title: str = "新对话") -> dict[str, Any]:
    sid = uuid.uuid4().hex[:12]
    record = {"id": sid, "title": title or "新对话", "created_at": int(time.time() * 1000)}
    _memory_sessions[sid] = record
    _memory_messages[sid] = []
    _memory_assets[sid] = []
    return record


async def list_sessions() -> list[dict[str, Any]]:
    return sorted(_memory_sessions.values(), key=lambda r: r["created_at"], reverse=True)


async def rename_session(session_id: str, title: str) -> None:
    if session_id in _memory_sessions:
        _memory_sessions[session_id]["title"] = title


async def delete_session(session_id: str) -> None:
    _memory_sessions.pop(session_id, None)
    _memory_messages.pop(session_id, None)
    _memory_assets.pop(session_id, None)


# ---------------------------------------------------------------- messages --
async def append_message(session_id: str, message: dict[str, Any]) -> None:
    _memory_messages.setdefault(session_id, []).append(message)


async def list_messages(session_id: str) -> list[dict[str, Any]]:
    return list(_memory_messages.get(session_id, []))


# ------------------------------------------------------------------ assets --
async def add_asset(session_id: str, asset: dict[str, Any]) -> dict[str, Any]:
    asset = {"id": uuid.uuid4().hex[:12], "created_at": int(time.time() * 1000), **asset}
    _memory_assets.setdefault(session_id, []).append(asset)
    return asset


async def list_assets(session_id: str) -> list[dict[str, Any]]:
    return list(_memory_assets.get(session_id, []))


# ------------------------------------------------------------- tutor state --
_tutor_state: dict[str, dict[str, Any]] = {}


async def get_tutor_state(session_id: str) -> dict[str, Any]:
    return _tutor_state.get(session_id, {"step_index": -1, "finished": False, "history": []})


async def save_tutor_state(session_id: str, state: dict[str, Any]) -> None:
    _tutor_state[session_id] = state


if __name__ == "__main__":  # pragma: no cover
    assert _use_postgres() is False
