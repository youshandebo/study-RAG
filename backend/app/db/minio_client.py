# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""对象存储客户端：MinIO / S3 存放录音原件与板书大图。
未配置 MinIO 时，素材以本地静态目录承载（开发模式），对外仍暴露统一 HTTP URL。"""
from __future__ import annotations

import asyncio
import base64
import pathlib
import re
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


async def delete_object(url: str) -> bool:
    """删除 `put_object` 上传的对象（MinIO 对象或本地静态文件），返回是否删除成功。

    给入库失败补偿用，三条契约必须钉死：

    1. **永不抛异常**。调用方一定在 `except` 分支里——清理动作自身失败（MinIO
       抖动、权限不足）再抛一个错，就会把真正要看的原始异常顶掉，排障时看到的
       变成"错误的原因的原因"。所以这里任何异常都吞掉，只以返回值表态。
    2. **幂等**。重复补偿（如重试路径两次进入 except）不能炸，删了和没得删
       都返回成功语义。
    3. **白名单**。本地分支只认 `_write_local` 生成的 `/static/{audio|boards}/<name>`
       形态，文件名限定安全字符集；任何带分隔符或相对路径成分的 URL 一律拒绝。
       补偿入口一旦变成"任意路径删除"，就是比资源泄漏严重得多的安全漏洞。

    MinIO 不可达时返回 False：残留对象只能靠离线 GC 兜底，本接口保证的是
    **正常路径零泄漏**，不承诺异常 infrastructure 下的绝对一致性。
    """
    if not url:
        return False
    if url.startswith(("http://", "https://")):
        return await asyncio.to_thread(_delete_minio, url)
    return await asyncio.to_thread(_delete_local, url)


# put_object 生成的本地文件名形态：`{uuid10}{ext}`，一律安全字符集。
# 补偿入口是唯一能根据 URL 删文件的地方，字符集白名单是它的边界。
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _delete_local(url: str) -> bool:
    """删除本地静态文件；只接受白名单前缀 + 安全文件名，其余一律不动。"""
    for prefix, directory in (("/static/audio/", AUDIO_DIR), ("/static/boards/", BOARDS_DIR)):
        if url.startswith(prefix):
            name = url[len(prefix):]
            # 含 `/` 或 `..` 的名称直接拒：宁可留下孤儿，也不能让补���路径越界删文件
            if not _SAFE_NAME.fullmatch(name):
                return False
            try:
                (directory / name).unlink(missing_ok=True)  # missing_ok=True → 幂等
            except OSError:
                return False
            return True
    return False


def _delete_minio(url: str) -> bool:
    """按 `put_object` 生成的 URL 反解 bucket/对象键并删除。

    只认当前配置端点下的 URL：端点被改过（URL 与配置不匹配）时宁可不删，
    也不要拿着旧路径去动一个我们不认识的桶。
    """
    settings = get_settings()
    if not settings.minio_endpoint:
        return False
    marker = f"http://{settings.minio_endpoint}/"
    if not url.startswith(marker):
        return False
    bucket, sep, obj_path = url[len(marker):].partition("/")
    if not sep or not bucket or not obj_path:
        return False
    try:
        from minio import Minio

        client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=False,
        )
        client.remove_object(bucket, obj_path)
        return True
    except Exception:
        return False
