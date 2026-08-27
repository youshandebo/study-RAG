"""音画证据切片调取与回放数据接口。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

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


def _synth_waveform(text: str) -> list[int]:
    """演示用合成波形能量包络（0~100），真实部署替换为音频文件的预计算波形。"""
    import hashlib

    seed = int(hashlib.md5(text.encode()).hexdigest(), 16)
    amps: list[int] = []
    for i in range(48):
        seed = (seed * 1103515245 + 12345) % (2**31)
        amps.append(20 + seed % 75)
    return amps
