"""users 增加 tenant_id（多租户隔离）

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-13

对应 `core/tenancy.py` 引入的租户归属字段。业务端点的隔离逻辑见
`db/vector_store.py`（payload 硬过滤）与 `services/rag/retriever.py`
（BM25 租户分区）；本列是**租户归属的权威来源**。

可空 + 无默认值是有意的：存量用户没有归属，`tenancy.resolve_tenant`
会把它收窄到默认租户（fail-closed），而不是放宽为全库可见。

若存量库是用 `create_all` 建的，本列可能已由
`relational._apply_schema_patches()` 补上。此时先 `alembic stamp 0001`
再 `upgrade head` 会因"列已存在"报错——用下面这条更稳：
    alembic stamp head      # 直接把库标记为最新（schema 已一致）
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("tenant_id", sa.String(64), nullable=True))
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_users_tenant_id", table_name="users")
    op.drop_column("users", "tenant_id")
