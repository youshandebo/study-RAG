#!/bin/sh
# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
# 后端镜像入口：先对齐 schema，再把主进程交给 CMD。
#
# 为什么放在 ENTRYPOINT 而不是 CMD
# -------------------------------
# 迁移必须**串行发生**在服务对外之前，且失败要能阻断启动。若写进 CMD，
# 换 CMD 就绕过了迁移；写成 ENTRYPOINT + exec "$@" 则"迁移 → 启动"是强绑定，
# 改 CMD 只改启动命令、改不掉迁移这一步。
#
# set -e：迁移失败 → 入口脚本非 0 退出 → 容器进入重启循环，
# 问题暴露在日志里，而不是带着未演进的 schema 对外服务。
set -e

cd /app

if [ "${AUTO_MIGRATE:-1}" = "0" ]; then
  echo "[entrypoint] AUTO_MIGRATE=0，跳过 schema 迁移"
else
  echo "[entrypoint] 对齐数据库 schema（alembic upgrade head）..."
  python scripts/migrate.py
fi

exec "$@"
