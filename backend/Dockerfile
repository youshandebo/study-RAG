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

# 启动即对齐 schema：ENTRYPOINT 串行执行 alembic upgrade head（见 docker-entrypoint.sh），
# 失败则容器退出进入重启循环（明确暴露），而不是带着旧 schema 对外服务。
# AUTO_MIGRATE=0 可关闭（交给手工控库的部署）。
RUN chmod +x docker-entrypoint.sh
ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
