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


class MessageRow(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[int] = mapped_column(BigInteger, index=True, default=0)
    payload: Mapped[str] = mapped_column(Text)  # PolymorphicMessage JSON 全文


class AssetRow(Base):
    __tablename__ = "assets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[int] = mapped_column(BigInteger, index=True)
    payload: Mapped[str] = mapped_column(Text)  # 资产元数据 JSON
