# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
# 单容器镜像（New API 式体验）：
#   docker run -d --name studay-rag --restart always \
#     --memory=768m -p 3000:3000 -v studay-data:/app/data \
#     ghcr.io/youshandebo/studay-rag:latest
# 前端(:3000) + 后端(内部:8000) + SQLite 持久化(/app/data)，首次访问网页初始化向导。
#
# 内存优化（小服务器友好，整机 RSS ≈ 300~400MB）：
# - 基础镜像 python:3.11-slim（Node 侧用 n=smallest 官方发行版）
# - uvicorn 单 worker + --limit-concurrency 限并发（重负载下排队而非 OOM）
# - Node server.js 默认低内存（standalone 产物 ~60MB RSS，堆上限 192MB 可用
#   NODE_MAX_OLD_SPACE 覆盖）
# - MALLOC_ARENA_MAX=2 抑制 glibc 多 arena 虚拟内存膨胀
# - 容器级内存上限建议 --memory=768m（含 Python 峰值余量，低于此易被 OOM-kill）

FROM node:20-bookworm-slim AS fe-builder
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend .
ARG NEXT_PUBLIC_API_BASE=/api/proxy
ENV NEXT_PUBLIC_API_BASE=$NEXT_PUBLIC_API_BASE
RUN npm run build

FROM python:3.11-slim AS runtime
# ffmpeg（音频压缩/切片）+ libmagic1（魔数校验）；Node 从 fe-builder 阶段取二进制
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libmagic1 curl ca-certificates nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# 后端：单一依赖来源 requirements.txt（曾因手列包清单漏 uvicorn 致 502）
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend/app ./backend/app

# 前端：standalone 产物（node:20-bookworm-slim 的 nodejs 即 Debian node 18+，兼容）
COPY --from=fe-builder /app/.next/standalone ./frontend
COPY --from=fe-builder /app/.next/static ./frontend/.next/static

COPY docker/start.sh /start.sh
RUN chmod +x /start.sh && mkdir -p /app/data

VOLUME ["/app/data"]
ENV BACKEND_ORIGIN=http://127.0.0.1:8000/api/v1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0 \
    UVICORN_WORKERS=1 \
    UVICORN_LIMIT_CONCURRENCY=24 \
    MALLOC_ARENA_MAX=2
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/api/v1/health || exit 1
CMD ["/start.sh"]
