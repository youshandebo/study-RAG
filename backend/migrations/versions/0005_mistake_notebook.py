"""错题本 mistake_notebook + 会话挂起自测题 pending_quiz_json

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-14

P2-B：CONVERGING 自测闭环与错题本体系。

两张改动
--------
1. `mistake_notebook`：错题条目 + Leitner 复习调度元数据。
   双重隔离——`tenant_id`（硬边界）+ `user_id`（学生之间互不可见）。
2. `socratic_sessions.pending_quiz_json`：CONVERGING 出题后**挂起等待作答**的
   自测题。存进会话行而非进程内存，否则负载均衡到别的副本就会
   "找不到题 → 无法判分"。

索引说明：`ix_mistake_tenant_user_status` 支撑"学生查自己错题"，
`ix_mistake_next_review` 支撑"到期待复习扫描"，
`ix_mistake_tenant_concept` 支撑"教研侧按知识点聚合卡点"。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite 不支持带默认值的复杂 ALTER，batch 模式对 SQLite/PG 都安全
    with op.batch_alter_table("socratic_sessions") as batch:
        batch.add_column(sa.Column("pending_quiz_json", sa.Text(), nullable=False, server_default=""))

    op.create_table(
        "mistake_notebook",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("course_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("concept_tag", sa.String(200), nullable=False, server_default=""),
        sa.Column("source_type", sa.String(30), nullable=False, server_default="passive_converge"),
        sa.Column("session_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("trace_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("question_context", sa.Text(), nullable=False, server_default=""),
        sa.Column("reference_answer", sa.Text(), nullable=False, server_default=""),
        sa.Column("options_json", sa.Text(), nullable=False, server_default=""),
        sa.Column("misconception", sa.Text(), nullable=False, server_default=""),
        sa.Column("leitner_box", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("mastery_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("review_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_reviewed_at", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("next_review_at", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.create_index("ix_mistake_notebook_tenant_id", "mistake_notebook", ["tenant_id"])
    op.create_index("ix_mistake_notebook_status", "mistake_notebook", ["status"])
    op.create_index("ix_mistake_notebook_created_at", "mistake_notebook", ["created_at"])
    op.create_index("ix_mistake_tenant_user_status", "mistake_notebook", ["tenant_id", "user_id", "status"])
    op.create_index("ix_mistake_next_review", "mistake_notebook", ["next_review_at"])
    op.create_index("ix_mistake_tenant_concept", "mistake_notebook", ["tenant_id", "concept_tag"])


def downgrade() -> None:
    for idx in (
        "ix_mistake_tenant_concept",
        "ix_mistake_next_review",
        "ix_mistake_tenant_user_status",
        "ix_mistake_notebook_created_at",
        "ix_mistake_notebook_status",
        "ix_mistake_notebook_tenant_id",
    ):
        op.drop_index(idx, table_name="mistake_notebook")
    op.drop_table("mistake_notebook")

    with op.batch_alter_table("socratic_sessions") as batch:
        batch.drop_column("pending_quiz_json")
