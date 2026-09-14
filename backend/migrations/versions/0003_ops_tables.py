"""运营后台台账表 + users.role

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-14

P1-C 运营后台最小交付所需的表：

- tenant_accounts  租户账户（累计发放额度 / 档位与日上限覆盖 / 冻结标志）
- balance_txns     不可变充值流水（幂等键唯一索引防后台双击重复充值）
- usage_events     单次问答用量事件（FinOps 聚合数据源）
- feedback_events  bad-case（显式点踩 + 隐式异常统一收口）

以及 users.role：区分 member / tenant_admin。平台超管不落这张表——
它用管理口令换 admin JWT 鉴权，见 api/v1/admin.py。

设计约束：这些表只服务运营侧，**不参与对话主链路的写路径**
（用量事件是流结束后一次性追加），避免运营侧写放大拖慢问答延迟。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("role", sa.String(20), nullable=True))

    op.create_table(
        "tenant_accounts",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("label", sa.String(200), nullable=False, server_default=""),
        sa.Column("tier_override", sa.String(20), nullable=True),
        sa.Column("daily_cap_override", sa.Integer(), nullable=True),
        sa.Column("granted_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("frozen", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.create_index("ix_tenant_accounts_created_at", "tenant_accounts", ["created_at"])

    op.create_table(
        "balance_txns",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("balance_before", sa.Integer(), nullable=False),
        sa.Column("balance_after", sa.Integer(), nullable=False),
        sa.Column("operator_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("idempotency_key", sa.String(100), nullable=True),
        sa.Column("memo", sa.String(500), nullable=False, server_default=""),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
    )
    op.create_index("ix_balance_txns_tenant_id", "balance_txns", ["tenant_id"])
    op.create_index("ix_balance_txns_created_at", "balance_txns", ["created_at"])
    op.create_index("ix_balance_txns_idempotency_key", "balance_txns", ["idempotency_key"], unique=True)

    op.create_table(
        "usage_events",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("session_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("message_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("provider", sa.String(50), nullable=False, server_default=""),
        sa.Column("model", sa.String(100), nullable=False, server_default=""),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("credits", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("degraded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
    )
    op.create_index("ix_usage_events_tenant_id", "usage_events", ["tenant_id"])
    op.create_index("ix_usage_events_session_id", "usage_events", ["session_id"])
    op.create_index("ix_usage_events_created_at", "usage_events", ["created_at"])

    op.create_table(
        "feedback_events",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("message_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("trace_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("query", sa.Text(), nullable=False, server_default=""),
        sa.Column("retrieved_json", sa.Text(), nullable=False, server_default=""),
        sa.Column("model", sa.String(100), nullable=False, server_default=""),
        sa.Column("degraded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(50), nullable=False, server_default=""),
        sa.Column("verdict", sa.String(10), nullable=False, server_default=""),
        sa.Column("tags", sa.String(200), nullable=False, server_default=""),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("source", sa.String(20), nullable=False, server_default=""),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
    )
    op.create_index("ix_feedback_events_tenant_id", "feedback_events", ["tenant_id"])
    op.create_index("ix_feedback_events_session_id", "feedback_events", ["session_id"])
    op.create_index("ix_feedback_events_trace_id", "feedback_events", ["trace_id"])
    op.create_index("ix_feedback_events_created_at", "feedback_events", ["created_at"])


def downgrade() -> None:
    for idx, table in (
        ("ix_feedback_events_created_at", "feedback_events"),
        ("ix_feedback_events_trace_id", "feedback_events"),
        ("ix_feedback_events_session_id", "feedback_events"),
        ("ix_feedback_events_tenant_id", "feedback_events"),
    ):
        op.drop_index(idx, table_name=table)
    op.drop_table("feedback_events")

    for idx in ("ix_usage_events_created_at", "ix_usage_events_session_id", "ix_usage_events_tenant_id"):
        op.drop_index(idx, table_name="usage_events")
    op.drop_table("usage_events")

    for idx in ("ix_balance_txns_idempotency_key", "ix_balance_txns_created_at", "ix_balance_txns_tenant_id"):
        op.drop_index(idx, table_name="balance_txns")
    op.drop_table("balance_txns")

    op.drop_index("ix_tenant_accounts_created_at", table_name="tenant_accounts")
    op.drop_table("tenant_accounts")

    op.drop_column("users", "role")
