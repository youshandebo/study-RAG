"""苏格拉底状态机状态表 socratic_sessions

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-14

P2-A：把引导阶段（diagnosing/guiding/reflecting/converging/resolved/revealed）
与提示阶梯级别、卡住计数、越狱尝试计数等，从**进程内字典**升级为
**持久化的一等状态**。

为什么必须落库（而不是继续放内存）
------------------------------------
旧实现把 tutor 状态放在 `relational._tutor_state` 进程内字典里，
多副本部署时学生的引导阶段会随负载均衡在各副本间漂移
（副本 A 记到 GUIDING，下个请求打到副本 B 又回到 DIAGNOSING）。
阶段是典型的"跨轮次续接"状态，必须由共享存储承载。

表结构对齐 `app/db/pg_models.py::SocraticSessionRow`，两者不得漂移。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "socratic_sessions",
        sa.Column("session_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False, server_default="public"),
        sa.Column("user_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("course_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("concept_tag", sa.String(200), nullable=False, server_default=""),
        sa.Column("phase", sa.String(20), nullable=False, server_default="diagnosing"),
        sa.Column("hint_level", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("turns_in_phase", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stuck_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("escape_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("question_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("converge_failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("guard_blocked", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_signal", sa.String(20), nullable=False, server_default="none"),
        sa.Column("history_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.create_index("ix_socratic_sessions_tenant_id", "socratic_sessions", ["tenant_id"])
    op.create_index("ix_socratic_sessions_user_id", "socratic_sessions", ["user_id"])
    op.create_index("ix_socratic_sessions_phase", "socratic_sessions", ["phase"])
    op.create_index("ix_socratic_sessions_created_at", "socratic_sessions", ["created_at"])


def downgrade() -> None:
    for idx in (
        "ix_socratic_sessions_created_at",
        "ix_socratic_sessions_phase",
        "ix_socratic_sessions_user_id",
        "ix_socratic_sessions_tenant_id",
    ):
        op.drop_index(idx, table_name="socratic_sessions")
    op.drop_table("socratic_sessions")
