# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""对象存储客户端：MinIO / S3 存放录音原件与板书大图。
未配置 MinIO 时，素材以本地静态目录承载（开发模式），对外仍暴露统一 HTTP URL。"""
from __future__ import annotations

import asyncio
import base64
import pathlib
import uuid

from app.core.config import get_settings

STATIC_DIR = pathlib.Path(__file__).resolve().parent.parent / "static"
BOARDS_DIR = STATIC_DIR / "boards"
AUDIO_DIR = STATIC_DIR / "audio"


def ensure_static_dirs() -> None:
    BOARDS_DIR.mkdir(parents=True, exist_ok=True)
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)


def _put_minio(kind: str, raw: bytes, name: str) -> str | None:
    """同步 MinIO SDK 调用；失败返回 None，由调用方回落到本地静态目录。"""
    settings = get_settings()
    if not settings.minio_endpoint:
        return None
    try:
        from minio import Minio
        import io

        client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=False,
        )
        bucket = "classroom-assets"
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
        obj_path = f"{kind}/{name}"
        client.put_object(bucket, obj_path, io.BytesIO(raw), len(raw))
        return f"http://{settings.minio_endpoint}/{bucket}/{obj_path}"
    except Exception:
        return None


def _write_local(kind: str, raw: bytes, name: str) -> str:
    target_dir = AUDIO_DIR if kind == "audio" else BOARDS_DIR
    (target_dir / name).write_bytes(raw)
    return f"/static/{'audio' if kind == 'audio' else 'boards'}/{name}"


async def put_object(kind: str, filename: str, content_b64: str) -> str:
    """上传二进制资产，返回可公网访问的 URL。MinIO 缺失时落盘本地 static 目录。

    MinIO SDK 与磁盘写入都是同步阻塞操作，一律走线程执行——否则在 async 路由里
    直接调用会卡住事件循环，并发上传时整个服务假死。
    """
    ensure_static_dirs()
    raw = base64.b64decode(content_b64) if content_b64 else b""
    ext = pathlib.Path(filename).suffix or (".wav" if kind == "audio" else ".png")
    name = f"{uuid.uuid4().hex[:10]}{ext}"

    url = await asyncio.to_thread(_put_minio, kind, raw, name)
    if url:
        return url
    return await asyncio.to_thread(_write_local, kind, raw, name)
