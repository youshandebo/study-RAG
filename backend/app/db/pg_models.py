# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""Postgres ORM 模型（SQLAlchemy 2.0 声明式，仅配置 POSTGRES_DSN 时使用）。"""
from __future__ import annotations

from sqlalchemy import BigInteger, Index, Integer, String, Text
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


# ------------------------------------------------------- 苏格拉底状态机 ----
# P2-A：把"当前处于哪个引导阶段 / 脚手架升到第几级 / 能否揭晓答案"
# 变成**可持久化**的一等状态。此前 tutor 状态只存在进程内字典里，
# 多副本部署下"每个副本各记一份"，学生的引导进度会随负载均衡漂移。
# 这张表让阶段流转变成跨请求、跨副本一致的权威状态。


class SocraticSessionRow(Base):
    """苏格拉底引导会话的 FSM 状态（每会话一行）。"""

    __tablename__ = "socratic_sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True, default="public")
    user_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    course_id: Mapped[str] = mapped_column(String(64), default="")
    concept_tag: Mapped[str] = mapped_column(String(200), default="")

    phase: Mapped[str] = mapped_column(String(20), default="diagnosing", index=True)
    hint_level: Mapped[int] = mapped_column(Integer, default=0)
    turns_in_phase: Mapped[int] = mapped_column(Integer, default=0)
    stuck_count: Mapped[int] = mapped_column(Integer, default=0)      # 最高级下仍卡住的次数
    escape_attempts: Mapped[int] = mapped_column(Integer, default=0)  # 越狱尝试次数（可观测）
    question_count: Mapped[int] = mapped_column(Integer, default=0)
    converge_failed: Mapped[int] = mapped_column(Integer, default=0)  # 1 = 自测失败过（错题信号）
    guard_blocked: Mapped[int] = mapped_column(Integer, default=0)
    last_signal: Mapped[str] = mapped_column(String(20), default="none")
    history_json: Mapped[str] = mapped_column(Text, default="[]")
    # 挂起中的自测题（CONVERGING 阶段出题后等待学生作答；判分后清空）。
    # 存进会话行而不是进程内存：判分必须跨请求、跨副本读到同一道题，
    # 否则负载均衡到别的副本就会"找不到题 → 无法判分"。
    pending_quiz_json: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[int] = mapped_column(BigInteger, index=True)
    updated_at: Mapped[int] = mapped_column(BigInteger, default=0)


# ------------------------------------------------------------ 错题本 ----
# P2-B：错题的两路归集（引导自测失败被动归档 / 用户手动收藏 / 考试失分）
# 与 Leitner 复习调度。**跨租户 + 用户级双重隔离**：租户是硬边界，
# user_id 保证学生之间互不可见——错题属于个人学习数据，比知识库更私密。


class MistakeNotebookRow(Base):
    """错题本条目（每条 = 一个待掌握的知识点实例 + Leitner 复习调度元数据）。"""

    __tablename__ = "mistake_notebook"
    __table_args__ = (
        # 学生查自己的错题（最高频）：租户 + 用户 + 状态
        Index("ix_mistake_tenant_user_status", "tenant_id", "user_id", "status"),
        # 到期待复习扫描（定时/打开错题本时）：按 next_review_at
        Index("ix_mistake_next_review", "next_review_at"),
        # 教研侧按知识点聚合卡点：租户 + 概念标签
        Index("ix_mistake_tenant_concept", "tenant_id", "concept_tag"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), default="")
    course_id: Mapped[str] = mapped_column(String(64), default="")
    concept_tag: Mapped[str] = mapped_column(String(200), default="")

    # passive_converge（引导自测失败被动归档）| active_manual（手动收藏）| exam_failed
    source_type: Mapped[str] = mapped_column(String(30), default="passive_converge")
    session_id: Mapped[str] = mapped_column(String(64), default="")
    trace_id: Mapped[str] = mapped_column(String(64), default="")

    question_context: Mapped[str] = mapped_column(Text, default="")   # 题目 / 当时的上下文
    reference_answer: Mapped[str] = mapped_column(Text, default="")    # 判分依据（复习时按它判）
    options_json: Mapped[str] = mapped_column(Text, default="")        # 客观题选项（判分用）
    misconception: Mapped[str] = mapped_column(Text, default="")       # 诊断出的误区

    leitner_box: Mapped[int] = mapped_column(Integer, default=1)        # 1..5
    mastery_score: Mapped[int] = mapped_column(Integer, default=0)      # 0..100
    review_count: Mapped[int] = mapped_column(Integer, default=0)
    last_reviewed_at: Mapped[int] = mapped_column(BigInteger, default=0)
    next_review_at: Mapped[int] = mapped_column(BigInteger, default=0)

    status: Mapped[str] = mapped_column(String(20), default="active", index=True)  # active|mastered|archived

    created_at: Mapped[int] = mapped_column(BigInteger, index=True)
    updated_at: Mapped[int] = mapped_column(BigInteger, default=0)
