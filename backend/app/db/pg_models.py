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
    # 租户归属：知识库最外层隔离边界（多租户模式下由运营后台分配）。
    # 可空 + 默认空串 = 存量库安全：老用户没有归属时回落默认租户。
    tenant_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    # 角色：member（默认）| tenant_admin（租户管理员，可导出本租户 bad-case）
    # 平台超管不走这里——它用管理口令换的 admin JWT 鉴权（见 api/v1/admin.py）。
    role: Mapped[str | None] = mapped_column(String(20), default="member", nullable=True)


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


# ------------------------------------------------------------------ 运营台账 ----
# 以下四张表只服务于 P1-C 运营后台：租户账户 / 充值流水 / 用量事件 / bad-case。
# 与业务表（sessions/messages/assets）解耦：运营侧写放大不应影响对话链路。


class TenantAccountRow(Base):
    """租户账户：平台侧给机构开的账，成员消费从这里出。

    `granted_total` 是**累计发放**额度（含人工调减的负数），不是"实时余额"——
    实时余额在计费账本里（`core/billing.py`，多副本共享）。
    这里只记"一共发过多少"，与 `balance_txns` 对账用。
    """

    __tablename__ = "tenant_accounts"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(String(200), default="")
    tier_override: Mapped[str | None] = mapped_column(String(20), nullable=True)
    daily_cap_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    granted_total: Mapped[int] = mapped_column(Integer, default=0)
    frozen: Mapped[int] = mapped_column(Integer, default=0)     # 1 = 冻结消费
    created_at: Mapped[int] = mapped_column(BigInteger, index=True)
    updated_at: Mapped[int] = mapped_column(BigInteger, default=0)


class BalanceTxnRow(Base):
    """不可变充值流水：只追加，绝不 UPDATE / DELETE。

    `idempotency_key` 唯一索引是**防后台双击重复充值**的最后一道闸——
    幂等不能只靠"先查一遍"，那在并发下等于没有。
    """

    __tablename__ = "balance_txns"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    amount: Mapped[int] = mapped_column(Integer)                # 正数充值 / 负数调减
    balance_before: Mapped[int] = mapped_column(Integer)
    balance_after: Mapped[int] = mapped_column(Integer)
    operator_id: Mapped[str] = mapped_column(String(64), default="")
    idempotency_key: Mapped[str | None] = mapped_column(String(100), unique=True, index=True, nullable=True)
    memo: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[int] = mapped_column(BigInteger, index=True)


class UsageEventRow(Base):
    """单次问答的用量事件：FinOps 聚合的数据源。"""

    __tablename__ = "usage_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), default="")
    session_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    message_id: Mapped[str] = mapped_column(String(64), default="")
    provider: Mapped[str] = mapped_column(String(50), default="")
    model: Mapped[str] = mapped_column(String(100), default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    credits: Mapped[int] = mapped_column(Integer, default=0)    # 平台计费额度扣减
    degraded: Mapped[int] = mapped_column(Integer, default=0)   # 1 = 走过降级/熔断
    created_at: Mapped[int] = mapped_column(BigInteger, index=True)


class FeedbackRow(Base):
    """Bad-case：显式反馈（点踩）与隐式异常（熔断/零召回/超时）统一收口。"""

    __tablename__ = "feedback_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    session_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    message_id: Mapped[str] = mapped_column(String(64), default="")
    trace_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    query: Mapped[str] = mapped_column(Text, default="")
    retrieved_json: Mapped[str] = mapped_column(Text, default="")  # [{chunk_id, score}]
    model: Mapped[str] = mapped_column(String(100), default="")
    degraded: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str] = mapped_column(String(50), default="")
    verdict: Mapped[str] = mapped_column(String(10), default="")   # up | down | ""
    tags: Mapped[str] = mapped_column(String(200), default="")     # 逗号分隔
    note: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(20), default="")    # explicit | implicit
    created_at: Mapped[int] = mapped_column(BigInteger, index=True)
