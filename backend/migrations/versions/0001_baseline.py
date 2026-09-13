"""baseline: sessions / users / messages / assets

Revision ID: 0001
Revises:
Create Date: 2026-09-13

这是**基线**，对应项目最初通过 `Base.metadata.create_all` 建出的四张表。
刻意不包含后续新增的列（如 users.tenant_id）—— 迁移脚本的价值就在于
忠实表达 schema 的演进历史，把新列塞进基线会让存量库无法对齐。

存量库接入（表已存在）：
    alembic stamp 0001       # 标记为基线，不重复建表
    alembic upgrade head     # 再跑后续增量
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("subject", sa.String(50), nullable=True),
        sa.Column("course_id", sa.String(64), nullable=True),
        sa.Column("chapter", sa.String(200), nullable=True),
        sa.Column("retrieval_mode", sa.String(20), nullable=True),
        sa.Column("time_alpha_override", sa.Integer(), nullable=True),
        sa.Column("owner", sa.String(32), nullable=True),
    )
    op.create_index("ix_sessions_created_at", "sessions", ["created_at"])
    op.create_index("ix_sessions_owner", "sessions", ["owner"])

    op.create_table(
        "users",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("email", sa.String(200), nullable=False),
        sa.Column("password_hash", sa.String(200), nullable=False),
        sa.Column("tier", sa.String(20), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index("ix_users_tier", "users", ["tier"])
    op.create_index("ix_users_created_at", "users", ["created_at"])

    op.create_table(
        "messages",
        # Integer（非 BigInteger）：SQLite 仅对 INTEGER 主键启用自增别名
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.String(32), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
    )
    op.create_index("ix_messages_session_id", "messages", ["session_id"])
    op.create_index("ix_messages_created_at", "messages", ["created_at"])

    op.create_table(
        "assets",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("session_id", sa.String(32), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("owner", sa.String(32), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
    )
    op.create_index("ix_assets_session_id", "assets", ["session_id"])
    op.create_index("ix_assets_created_at", "assets", ["created_at"])
    op.create_index("ix_assets_owner", "assets", ["owner"])


def downgrade() -> None:
    op.drop_table("assets")
    op.drop_table("messages")
    op.drop_table("users")
    op.drop_table("sessions")
