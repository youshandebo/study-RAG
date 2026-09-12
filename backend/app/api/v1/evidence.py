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


def _resolve_audio_source(assets: list[dict] | None, audio_id: str) -> pathlib.Path | None:
    """按 audio_id 精确定位该切片所属的录音文件。

    历史 bug：此前遍历全部资产、"存在即覆盖"地取最后一份，多份录音共存时
    任何切片都会播放最后上传的那份录音（串台）。现在优先按 audio_id 精确命中；
    对缺少 audio_id 的旧数据，仅当库内只有一份录音（无歧义）时才回退，
    多份且无法判定时返回 None，由调用方给 404 而不是猜一个。
    """
    candidates: list[pathlib.Path] = []
    for asset in assets or []:
        if asset.get("kind") != "audio":
            continue
        uri = str(asset.get("uri") or "")
        if not uri.startswith("/static/audio/"):
            continue
        candidate = AUDIO_DIR / pathlib.Path(uri).name
        if not candidate.exists():
            continue
        if str(asset.get("audio_id") or "") == audio_id:
            return candidate  # 精确命中
        candidates.append(candidate)
    # 旧数据兜底：库内只有一份录音时不存在歧义
    uniq = {str(p) for p in candidates}
    return candidates[0] if len(uniq) == 1 else None


@router.get("/evidence/audio/{chunk_id}/slice")
async def evidence_audio_slice(chunk_id: str, start_ms: int = 0, end_ms: int = 0):
    """毫秒级按需切片：ffmpeg -ss/-to -c copy 无损快速切分，StreamingResponse 流式返回短片段。

    - src 参数必须是 /static/audio/ 下的本地文件 URL（MinIO 部署请走对象存储直链）
    - 切片长度限制 [0.2s, 120s]，防止借接口整段拉取
    - 演示模式的预置切片（lec-*）没有真实音频文件，返回 404 语义化提示
    """
    from app.services.media import compressor

    duration_s = (end_ms - start_ms) / 1000
    if start_ms < 0 or end_ms <= start_ms or duration_s > 120 or duration_s < 0.2:
        raise HTTPException(status_code=400, detail="切片区间非法（需 0 ≤ start < end，且长度 0.2~120 秒）")
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

    # 在已入库资产里反查音频文件（走仓库 API，PG/内存双模式一致）
    import app.db.relational as repo

    src_path = _resolve_audio_source(await repo.list_all_assets(), audio_id)
    if src_path is None:
        raise HTTPException(
            status_code=404,
            detail="未定位到该切片所属的录音文件（多份录音共存且缺少关联信息时不做猜测），请重新上传该课堂录音后重试",
        )

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
