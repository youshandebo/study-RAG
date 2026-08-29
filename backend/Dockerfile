# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
FROM python:3.11-slim

WORKDIR /app

# 系统依赖：ffmpeg（音频压缩/毫秒级切片）、libmagic1（文件魔数校验）
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libmagic1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
