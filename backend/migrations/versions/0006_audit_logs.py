"""审计日志 audit_logs

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-15

P3：把"谁在什么时间对哪个租户做了什么敏感操作"变成可追溯记录。

为什么值得单独一张表
--------------------
系统此前只有**业务结果**：余额流水、用量事件、bad-case。能查到"某切片被设为
定版"，却查不到"是谁设的"——机构尽调与合规审计问的恰恰是后者。

与既有表的分工是"结果 vs 责任"，不是重复：
- `balance_txns`  钱怎么变的
- `usage_events`  资源怎么消耗的
- `audit_logs`    **谁**改的（本表）

只追加：本表没有任何 update/delete 代码路径，也不提供修改接口。

索引说明：`ix_audit_tenant_ts` 支撑"按租户追溯"（机构自查），
`ix_audit_action_ts` 支撑"按操作类型追溯"（如"所有充值操作"）。
两者都带 ts 是因为审计查询天然按时间倒序看最近发生了什么。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_logs",
        # 自增整型主键。注意必须用 `with_variant(Integer, "sqlite")`：
        # SQLite 只对 `INTEGER PRIMARY KEY` 自增，`BIGINT PRIMARY KEY` 会以 NULL 插入
        # 并撞 NOT NULL 约束（实测本机 SQLite 路径直接写入失败）。
        # Postgres 仍走 BIGINT，两边语义一致。
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer, "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("ts", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.String(64), nullable=False, server_default=""),
        sa.Column("actor", sa.String(128), nullable=False, server_default=""),
        sa.Column("actor_role", sa.String(20), nullable=False, server_default=""),
        sa.Column("tenant_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("target", sa.String(255), nullable=False, server_default=""),
        sa.Column("ip", sa.String(64), nullable=False, server_default=""),
        sa.Column("detail_json", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("ix_audit_logs_ts", "audit_logs", ["ts"])
    op.create_index("ix_audit_logs_actor", "audit_logs", ["actor"])
    op.create_index("ix_audit_logs_tenant_id", "audit_logs", ["tenant_id"])
    # 复合索引：租户/操作类型追溯都按时间倒序，单列索引无法覆盖排序
    op.create_index("ix_audit_tenant_ts", "audit_logs", ["tenant_id", "ts"])
    op.create_index("ix_audit_action_ts", "audit_logs", ["action", "ts"])


def downgrade() -> None:
    for idx in (
        "ix_audit_action_ts",
        "ix_audit_tenant_ts",
        "ix_audit_logs_tenant_id",
        "ix_audit_logs_actor",
        "ix_audit_logs_ts",
    ):
        op.drop_index(idx, table_name="audit_logs")
    op.drop_table("audit_logs")
