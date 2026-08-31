#!/bin/sh
# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
# 单容器启动：后端 uvicorn(127.0.0.1:8000, 单 worker 限并发) + 前端 node(3000)
# UVICORN_LIMIT_CONCURRENCY：小内存服务器防 OOM（超并发返回 503 排队重试）
#
# 守护策略（修复"后端崩了容器还活着 → 永久 502"）：
#   1. 先起后端并等待 /health 就绪再起前端，消除启动窗口期 502
#   2. 任一核心进程退出 → 整个容器退出，交由 --restart always 拉起
#      （此前 exec node 替换主进程后 watchdog 退出杀不死容器，后端死了前端还在）
set -e

# glibc 多 arena 虚拟内存膨胀抑制（多线程 Python 进程实测可省 20%+ RSS）
export MALLOC_ARENA_MAX=2

cd /app/backend
python -m uvicorn app.main:app \
  --host 127.0.0.1 --port 8000 \
  --workers "${UVICORN_WORKERS:-1}" \
  --limit-concurrency "${UVICORN_LIMIT_CONCURRENCY:-24}" \
  --timeout-keep-alive 30 \
  --no-access-log &
UVICORN_PID=$!

# 等后端就绪（最多 120s）；启动即崩（缺依赖等）则容器直接退出进入重启循环，问题可见而非静默 502
i=0
until curl -fsS http://127.0.0.1:8000/api/v1/health >/dev/null 2>&1; do
  i=$((i + 1))
  if ! kill -0 "$UVICORN_PID" 2>/dev/null; then
    echo "uvicorn exited during startup, container will restart" >&2
    exit 1
  fi
  if [ "$i" -ge 60 ]; then
    echo "backend not ready within 120s, container will restart" >&2
    exit 1
  fi
  sleep 2
done

cd /app/frontend
export NODE_OPTIONS="--max-old-space-size=${NODE_MAX_OLD_SPACE:-192}"
node server.js &
NODE_PID=$!

# 任一进程死 → 杀掉另一个并退出容器（docker restart 策略接管）
while kill -0 "$UVICORN_PID" 2>/dev/null && kill -0 "$NODE_PID" 2>/dev/null; do
  sleep 2
done
echo "core process (uvicorn/node) exited, container will restart" >&2
kill "$UVICORN_PID" "$NODE_PID" 2>/dev/null || true
exit 1
