# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
# 单容器镜像（New API 式体验）：
#   docker run -d -p 3000:3000 -v studay-data:/app/data ghcr.io/youshandebo/studay-rag:latest
# 前端(:3000) + 后端(内部:8000) + SQLite 持久化(/app/data)，首次访问网页初始化向导。
# 需要更强存储/检索时改用 docker-compose.prod.yml（Postgres/Qdrant/Redis/MinIO）。

FROM node:20-bookworm-slim AS fe-builder
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend .
ARG NEXT_PUBLIC_API_BASE=/api/proxy
ENV NEXT_PUBLIC_API_BASE=$NEXT_PUBLIC_API_BASE
RUN npm run build

FROM python:3.11-slim AS be-builder
WORKDIR /wheels
COPY backend/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM node:20-bookworm-slim AS runtime
# node:20-bookworm 自带 Node；补 Python3.11 / ffmpeg / libmagic（音频压缩、魔数校验）
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 python3-pip ffmpeg libmagic1 \
    && rm -rf /var/lib/apt/lists/*
RUN pip3 install --no-cache-dir --break-system-packages httpx "sqlalchemy[asyncio]" aiosqlite "pydantic>=2.6" "pydantic-settings>=2.2" python-multipart email-validator

WORKDIR /app
# 后端代码与依赖
COPY --from=be-builder /install /usr/local
COPY backend/requirements.txt ./backend/requirements.txt
COPY backend/app ./backend/app
# 前端 standalone 产物
COPY --from=fe-builder /app/.next/standalone ./frontend
COPY --from=fe-builder /app/.next/static ./frontend/.next/static

COPY docker/start.sh /start.sh
RUN chmod +x /start.sh

# 数据卷：SQLite + runtime_config + JWT 密钥 + 上传素材
VOLUME ["/app/data"]
ENV BACKEND_ORIGIN=http://127.0.0.1:8000/api/v1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0
EXPOSE 3000
CMD ["/start.sh"]
