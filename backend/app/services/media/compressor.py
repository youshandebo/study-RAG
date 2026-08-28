"""智能媒体压缩管道：图片 WebP 化（缩边/锐化/抹 EXIF）+ 音频单声道 Opus 压缩。

- 图片：Pillow 实现，纯 wheel 依赖即可用。
- 音频：基于 ffmpeg（-ac 1 -ar 24000 -b:a 24k Opus）。ffmpeg 未安装时优雅降级
  （原样返回并标注 skipped），安装后自动激活，无需改代码。
- 临时文件全部经 contextlib 上下文管理器管理，异常路径 100% 自动清理。
"""
from __future__ import annotations

import contextlib
import pathlib
import shutil
import subprocess
import tempfile

from app.core import runtime_config

DEFAULTS = {"image_quality": 82, "image_max_edge": 2560, "audio_bitrate": 24, "audio_max_mb": 200}


def media_settings() -> dict:
    cfg = runtime_config.effective("media")
    out = {**DEFAULTS}
    for key in DEFAULTS:
        try:
            out[key] = int(cfg.get(key) or DEFAULTS[key])
        except (TypeError, ValueError):
            pass
    return out


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


@contextlib.contextmanager
def temp_media_file(data: bytes, suffix: str):
    """流式分块写临时文件；无论正常返回还是异常退出都保证删除。"""
    fd = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        with fd as f:
            for i in range(0, len(data), 1 << 20):  # 1MB 分块写盘，避免大文件整体驻留内存
                f.write(data[i : i + (1 << 20)])
            f.flush()
        yield pathlib.Path(fd.name)
    finally:
        with contextlib.suppress(OSError):
            pathlib.Path(fd.name).unlink(missing_ok=True)


def compress_image(data: bytes) -> tuple[bytes, dict]:
    """上传图片统一转 WebP：抹 EXIF → 纠正方向 → 限最大边长 → 非锐化滤镜增强笔迹边缘。

    返回 (压缩后字节, 元信息)。WebP 编码失败时回落原格式重编码（仍抹 EXIF）。
    """
    from io import BytesIO

    from PIL import Image, ImageOps

    cfg = media_settings()
    quality, max_edge = cfg["image_quality"], cfg["image_max_edge"]
    img = Image.open(BytesIO(data))
    img = ImageOps.exif_transpose(img)  # 依 EXIF 纠正方向（EXIF 随后整体丢弃）
    if max(img.size) > max_edge:
        img.thumbnail((max_edge, max_edge), Image.LANCZOS)
    img = img.filter(__unsharp())

    out = BytesIO()
    try:
        img.save(out, "WEBP", quality=quality, method=4)
        fmt = "webp"
    except Exception:
        out = BytesIO()
        img.save(out, img.format or "PNG", quality=quality)  # 回落：仍无 EXIF
        fmt = (img.format or "PNG").lower()
    meta = {
        "format": fmt,
        "width": img.width,
        "height": img.height,
        "original_bytes": len(data),
        "compressed_bytes": out.tell(),
        "exif_stripped": True,
    }
    return out.getvalue(), meta


def __unsharp():
    from PIL import ImageFilter

    # 非锐化滤镜：增强手写公式与板书文字边缘（radius/percent/threshold 保守取值）
    return ImageFilter.UnsharpMask(radius=1.8, percent=90, threshold=3)


def compress_audio(data: bytes, src_suffix: str = ".wav") -> tuple[bytes, dict]:
    """课堂录音压缩：单声道 → 24kHz → Opus 24~32kbps（配置驱动）。

    Returns: (压缩后字节, 元信息)。ffmpeg 缺失或失败时返回原字节并标记 skipped 原因。
    """
    cfg = media_settings()
    bitrate = max(24, min(32, cfg["audio_bitrate"]))
    if not ffmpeg_available():
        return data, {"skipped": "ffmpeg 未安装", "original_bytes": len(data)}

    with temp_media_file(data, src_suffix or ".wav") as src:
        dst = src.with_suffix(".ogg")
        try:
            proc = subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(src),
                    "-ac", "1",            # 单声道
                    "-ar", "24000",        # 重采样 24kHz
                    "-c:a", "libopus",
                    "-b:a", f"{bitrate}k",
                    str(dst),
                ],
                capture_output=True, timeout=600,
            )
            if proc.returncode != 0:
                # 部分 ffmpeg 构建不含 libopus：回落 AAC(.m4a)
                dst = src.with_suffix(".m4a")
                proc = subprocess.run(
                    [
                        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-i", str(src), "-ac", "1", "-ar", "24000",
                        "-c:a", "aac", "-b:a", f"{max(32, bitrate + 8)}k", str(dst),
                    ],
                    capture_output=True, timeout=600,
                )
                if proc.returncode != 0:
                    return data, {"skipped": f"ffmpeg 失败: {proc.stderr[-120:].decode(errors='ignore')}"}
            compressed = dst.read_bytes()
        finally:
            with contextlib.suppress(OSError):
                dst.unlink(missing_ok=True)

    meta = {
        "codec": "opus" if dst.suffix == ".ogg" else "aac",
        "channels": 1,
        "sample_rate": 24000,
        "bitrate_kbps": bitrate if dst.suffix == ".ogg" else bitrate + 8,
        "original_bytes": len(data),
        "compressed_bytes": len(compressed),
    }
    return compressed, meta


def slice_audio(src: pathlib.Path, start_ms: int, end_ms: int) -> "subprocess.Popen[bytes] | None":
    """毫秒级按需切片：-ss/-to 快速定位 + -c copy 无损流式拷贝。

    返回 ffmpeg 进程（stdout 为流），调用方负责 StreamingResponse 消费与进程回收；
    ffmpeg 缺失返回 None。
    """
    if not ffmpeg_available():
        return None
    proc = subprocess.Popen(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", f"{start_ms / 1000:.3f}", "-to", f"{end_ms / 1000:.3f}",
            "-i", str(src),
            "-c", "copy",          # 无损快速切片
            "-f", "ogg", "pipe:1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc
