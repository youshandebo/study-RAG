# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""SQLAlchemy ORM 映射（可选依赖：无 Postgres 时业务层自动跳过持久化）。"""
from __future__ import annotations

try:
    from sqlalchemy import Column, Integer, JSON, String, Text, BigInteger
    from sqlalchemy.orm import declarative_base

    Base = declarative_base()

    class SessionEntity(Base):
        __tablename__ = "tutor_sessions"
        id = Column(String(64), primary_key=True)
        title = Column(String(256), default="新对话")
        created_at = Column(BigInteger, default=0)
        config = Column(JSON, default=dict)

    class MessageEntity(Base):
        __tablename__ = "tutor_messages"
        id = Column(String(64), primary_key=True)
        session_id = Column(String(64), index=True)
        role = Column(String(16))
        type = Column(String(32))
        content = Column(Text, default="")
        payload = Column(JSON, default=dict)
        created_at = Column(BigInteger, default=0)

    class AssetEntity(Base):
        __tablename__ = "tutor_assets"
        id = Column(String(64), primary_key=True)
        session_id = Column(String(64), index=True)
        kind = Column(String(32))
        uri = Column(Text, default="")
        meta = Column(JSON, default=dict)
        created_at = Column(BigInteger, default=0)

except ImportError:  # pragma: no cover - sqlalchemy 未安装时保持可导入
    Base = None  # type: ignore[assignment]

    class SessionEntity:  # type: ignore[no-redef]
        pass

    class MessageEntity:  # type: ignore[no-redef]
        pass

    class AssetEntity:  # type: ignore[no-redef]
        pass
