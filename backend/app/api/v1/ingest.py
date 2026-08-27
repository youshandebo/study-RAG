"""多模态资产入库流水线接口：录音/板书上传 → 异步 ASR/VLM/切片/向量化。"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, UploadFile, File, Form
from fastapi.responses import StreamingResponse

import app.db.relational as repo
from app.db.minio_client import put_object
from app.services.asr.hotwords import correct
from app.services.asr.transcriber import Transcriber
from app.services.extractor.difficulty import score
from app.services.extractor.pitfall import extract_from_chunks
from app.services.rag.aligner import align_boards
from app.services.rag.chunker import Chunk, chunk_transcript
from app.services.rag.retriever import get_retriever
from app.services.vlm.ocr_engine import OCREngine

router = APIRouter()


@router.post("/ingest")
async def ingest(
    session_id: str = Form(...),
    media_type: str = Form("audio"),  # audio | board
    file: UploadFile = File(...),
    lecture_date: str = Form(""),
):
    """同步执行轻量流水线（演示规模），返回处理摘要。重型部署走 workers/tasks.py 的 Celery 版本。"""
    raw = await file.read()
    import base64

    content_b64 = base64.b64encode(raw).decode()
    url = await put_object(media_type, file.filename or "upload", content_b64)

    if media_type == "audio":
        segments = await Transcriber().transcribe(raw, file.filename or "")
        for seg in segments:
            seg.text = correct(seg.text)
        audio_id = f"ing-{abs(hash(file.filename or 'audio')) % 10_000}"
        chunks = chunk_transcript(audio_id, segments)
        chunks = align_boards(chunks, board_count=12)
        for c in chunks:
            c.difficulty = score(c.text)
            c.pitfalls = []
        pitfalls = extract_from_chunks(chunks)
    else:
        ocr = await OCREngine().recognize(content_b64)
        text = ocr.get("problem_text") or ocr.get("latex", "")
        audio_id = "board-only"
        chunks = [
            Chunk(
                id=f"bd-{abs(hash(text)) % 100_000}",
                audio_id=audio_id,
                start="00:00",
                end="00:00",
                text=text,
                board_index=1,
                board_caption=f"板书上传 · {file.filename or ''}",
                exam_point="板书推导要点",
                difficulty=score(text),
            )
        ]
        pitfalls = []

    retriever = await get_retriever()
    added = await retriever.register_chunks(chunks)

    asset = await repo.add_asset(
        session_id,
        {
            "kind": media_type,
            "uri": url,
            "filename": file.filename or "",
            "lecture_date": lecture_date,
            "chunk_count": added,
            "pitfalls": pitfalls[:3],
        },
    )
    return {
        "asset": asset,
        "chunks_added": added,
        "pitfalls_extracted": pitfalls[:5],
        "sample_chunks": [
            {"start": c.start, "end": c.end, "text": c.text[:60], "exam_point": c.exam_point}
            for c in chunks[:4]
        ],
    }


@router.get("/assets")
async def assets(session_id: str) -> list[dict]:
    return await repo.list_assets(session_id)
