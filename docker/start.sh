#!/bin/sh
# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
# 单容器启动脚本：后端 uvicorn(内部 8000) + 前端 node(3000)
set -e

cd /app/backend
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000 &
UVICORN_PID=$!

cd /app/frontend
node server.js
