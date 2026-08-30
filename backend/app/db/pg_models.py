# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""Postgres ORM 模型（SQLAlchemy 2.0 声明式，仅配置 POSTGRES_DSN 时使用）。"""
from __future__ import annotations

from sqlalchemy import BigInteger, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SessionRow(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(String(200), default="新对话")
    created_at: Mapped[int] = mapped_column(BigInteger, index=True)
    subject: Mapped[str | None] = mapped_column(String(50), nullable=True)
    course_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    chapter: Mapped[str | None] = mapped_column(String(200), nullable=True)
    retrieval_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    time_alpha_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    owner: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    tier: Mapped[str] = mapped_column(String(20), default="free", index=True)
    created_at: Mapped[int] = mapped_column(BigInteger, index=True)


class MessageRow(Base):
    __tablename__ = "messages"

    # Integer（非 BigInteger）：SQLite 仅对 INTEGER 主键启用自增别名
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[int] = mapped_column(BigInteger, index=True, default=0)
    payload: Mapped[str] = mapped_column(Text)  # PolymorphicMessage JSON 全文


class AssetRow(Base):
    __tablename__ = "assets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[int] = mapped_column(BigInteger, index=True)
    payload: Mapped[str] = mapped_column(Text)  # 资产元数据 JSON
    owner: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
