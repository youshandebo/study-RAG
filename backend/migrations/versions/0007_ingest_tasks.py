"""入库任务表 ingest_tasks

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-17

把长耗时入库流水线从 HTTP 请求里摘出来：提交即返回任务句柄，进度与结果落库。

为什么必须单独一张表
--------------------
此前 `POST /ingest` 是同步契约，HTTP 连接在整个流水线期间被独占。链路动辄
几十秒到数分钟，由此产生三类无法靠"调大超时"解决的生产问题：

1. 网关超时必然发生（Nginx/CDN 默认 60s 断连）——用户看到 504，服务侧继续烧
   CPU，结果无人接收。
2. 断连后无法找回结果：没有任务句柄，重连后既查不到进度也拿不到结果，
   只能重传整个文件。
3. 重传无幂等：同一次上传被处理两遍，双份切片污染检索，配额被双倍扣除。

索引设计
--------
- `ix_ingest_tasks_idem`（unique）：幂等的闸。唯一索引才行，"先查再写"在
 并发下必然漏——两个副本同时收到同一 idempotency_key 的重传时，其中一个
 必须被数据库挡住，而不是靠应用层判断。
- `ix_ingest_tasks_session`：前端按会话拉取"本次上传"的任务列表。
- `ix_ingest_tasks_status_updated`：收尸扫描用的索引（查 updated_at 已过期的
  running 任务），僵尸任务必须能被标记失败，否则永远停在 running。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ingest_tasks",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("session_id", sa.String(32), nullable=False, server_default=""),
        sa.Column("owner", sa.String(32), nullable=True),
        sa.Column("tenant_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("idempotency_key", sa.String(64), nullable=False, server_default=""),
        sa.Column("media_type", sa.String(20), nullable=False, server_default="text"),
        sa.Column("filename", sa.String(255), nullable=False, server_default=""),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("created_at", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("finished_at", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("result_json", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("ix_ingest_tasks_created_at", "ingest_tasks", ["created_at"])
    op.create_index("ix_ingest_tasks_session", "ingest_tasks", ["session_id", "created_at"])
    op.create_index(
        "ix_ingest_tasks_status_updated", "ingest_tasks", ["status", "updated_at"]
    )
    op.create_index(
        "ix_ingest_tasks_idem",
        "ingest_tasks",
        ["tenant_id", "idempotency_key"],
        unique=True,
    )


def downgrade() -> None:
    for idx in (
        "ix_ingest_tasks_idem",
        "ix_ingest_tasks_status_updated",
        "ix_ingest_tasks_session",
        "ix_ingest_tasks_created_at",
    ):
        op.drop_index(idx, table_name="ingest_tasks")
    op.drop_table("ingest_tasks")
