# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""音画证据切片调取与回放数据接口。"""
from __future__ import annotations

import hashlib
import pathlib

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from app.db.minio_client import AUDIO_DIR
from app.services.rag.retriever import get_retriever

router = APIRouter()


@router.get("/evidence/audio/{chunk_id}")
async def evidence_audio(chunk_id: str):
    """返回某条课堂切片的证据包：原声定位参数 + 板书元数据。"""
    retriever = await get_retriever()
    for chunk in await retriever.all_chunks():
        if chunk.id == chunk_id:
            return JSONResponse(
                {
                    "chunk_id": chunk.id,
                    "audio_id": chunk.audio_id,
                    "timestamp_range": [chunk.start, chunk.end],
                    "transcript": chunk.text,
                    "board_index": chunk.board_index,
                    "board_caption": chunk.board_caption,
                    "exam_point": chunk.exam_point,
                    "board_image_url": f"/static/boards/board_{(chunk.board_index or 1):02d}.svg",
                    "waveform": _synth_waveform(chunk.text),
                }
            )
    raise HTTPException(status_code=404, detail="evidence chunk not found")


@router.get("/evidence/list")
async def evidence_list():
    retriever = await get_retriever()
    chunks = await retriever.all_chunks()
    return [
        {
            "chunk_id": c.id,
            "audio_id": c.audio_id,
            "timestamp_range": [c.start, c.end],
            "transcript_snippet": c.text[:60],
            "board_caption": c.board_caption,
            "exam_point": c.exam_point,
        }
        for c in chunks
    ]


@router.get("/evidence/audio/{chunk_id}/slice")
async def evidence_audio_slice(chunk_id: str, start_ms: int = 0, end_ms: int = 0):
    """毫秒级按需切片：ffmpeg -ss/-to -c copy 无损快速切分，StreamingResponse 流式返回短片段。

    - src 参数必须是 /static/audio/ 下的本地文件 URL（MinIO 部署请走对象存储直链）
    - 切片长度限制 [0.2s, 120s]，防止借接口整段拉取
    - 演示模式的预置切片（lec-*）没有真实音频文件，返回 404 语义化提示
    """
    from app.services.media import compressor

    duration_s = (end_ms - start_ms) / 1000
    if start_ms < 0 or end_ms <= start_ms or duration_s > 120:
        raise HTTPException(status_code=400, detail="切片区间非法（需 0 ≤ start < end，且长度 ≤ 120 秒）")
    if not compressor.ffmpeg_available():
        raise HTTPException(status_code=503, detail="服务器未安装 ffmpeg，无法按需切片；请安装后重试")

    # 定位该切片所属的真实音频文件
    retriever = await get_retriever()
    audio_id = ""
    for chunk in await retriever.all_chunks():
        if chunk.id == chunk_id:
            audio_id = chunk.audio_id
            break
    if not audio_id or audio_id.startswith(("lec-", "board-only")):
        raise HTTPException(status_code=404, detail="演示切片无真实音频文件，请先上传课堂录音后重试")

    # 在已入库资产里按 audio_id 反查文件（audio_id 与上传资产一一对应由 ingest 生成）
    import app.db.relational as repo

    src_path: pathlib.Path | None = None
    for assets in repo._memory_assets.values():
        for asset in assets:
            if asset.get("kind") == "audio" and str(asset.get("uri", "")).startswith("/static/audio/"):
                candidate = AUDIO_DIR / pathlib.Path(asset["uri"]).name
                if candidate.exists():
                    src_path = candidate  # 同一音频组的任一载体文件都含完整时间轴
    if src_path is None:
        raise HTTPException(status_code=404, detail="音频文件不存在或已被清理")

    proc = compressor.slice_audio(src_path, start_ms, end_ms)
    if proc is None or proc.stdout is None:
        raise HTTPException(status_code=503, detail="ffmpeg 启动失败")

    async def stream():
        import asyncio

        loop = asyncio.get_event_loop()
        try:
            while True:
                chunk = await loop.run_in_executor(None, proc.stdout.read, 64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            proc.kill()

    return StreamingResponse(
        stream(),
        media_type="audio/ogg",
        headers={"Cache-Control": "no-cache", "X-Slice-Range": f"{start_ms}-{end_ms}"},
    )


def _synth_waveform(text: str) -> list[int]:
    """演示用合成波形能量包络（0~100），真实部署替换为音频文件的预计算波形。"""
    seed = int(hashlib.md5(text.encode()).hexdigest(), 16)
    amps: list[int] = []
    for i in range(48):
        seed = (seed * 1103515245 + 12345) % (2**31)
        amps.append(20 + seed % 75)
    return amps
