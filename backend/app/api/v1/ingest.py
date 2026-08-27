"""多模态资产入库流水线接口：录音/板书/文字上传 → 异步 ASR/VLM/切片/向量化。"""
from __future__ import annotations

import base64
import re
import uuid

from fastapi import APIRouter, UploadFile, File, Form

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

TEXT_CHUNK_TARGET = 220  # 文字素材单切片目标字数


def _split_text_chunks(text: str) -> list[str]:
    """把粘贴的长文按段落聚合为教学切片：优先空行分段，长段落再按句切窗。"""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    units: list[str] = []
    for para in paragraphs:
        if len(para) <= TEXT_CHUNK_TARGET:
            units.append(para)
            continue
        sentences = re.split(r"(?<=[。！？!?；;])", para)
        buf = ""
        for sent in sentences:
            if buf and len(buf) + len(sent) > TEXT_CHUNK_TARGET:
                units.append(buf)
                buf = sent.lstrip()
            else:
                buf += sent
        if buf.strip():
            units.append(buf)
    # 相邻过短片段合并，避免碎片切片
    merged: list[str] = []
    for u in units:
        if merged and len(merged[-1]) + len(u) < 60:
            merged[-1] = f"{merged[-1]}\n{u}"
        else:
            merged.append(u)
    return merged[:60]


@router.post("/ingest")
async def ingest(
    session_id: str = Form(...),
    media_type: str = Form("audio"),  # audio | board | text
    file: UploadFile | None = File(None),
    lecture_date: str = Form(""),
    text_content: str = Form(""),
):
    """同步执行轻量流水线（演示规模），返回处理摘要。重型部署走 workers/tasks.py 的 Celery 版本。

    - audio  上传录音文件 → ASR 转录 → 切片对齐
    - board  上传板书图片 → VLM 识别 → 入库
    - text   直接提交文字素材（text_content 表单字段，或 .txt/.md 文件）→ 切片入库
    """
    raw = await file.read() if file is not None else b""
    content_b64 = base64.b64encode(raw).decode()

    if media_type == "text":
        note_text = (text_content or "").strip()
        filename = file.filename or "" if file else ""
        if not note_text and raw:
            try:
                note_text = raw.decode("utf-8")
            except UnicodeDecodeError:
                from fastapi import HTTPException

                raise HTTPException(status_code=400, detail="文字文件需为 UTF-8 编码的 txt/md") from None
        note_text = note_text.strip()
        if not note_text:
            from fastapi import HTTPException

            raise HTTPException(status_code=400, detail="文字内容为空")

        url = await put_object("boards", filename or f"note-{uuid.uuid4().hex[:6]}.txt",
                               base64.b64encode(note_text.encode()).decode())
        chunks = _build_text_chunks(note_text, filename or "文字笔记")
        pitfalls = extract_from_chunks(chunks)

        retriever = await get_retriever()
        added = await retriever.register_chunks(chunks)
        asset = await repo.add_asset(
            session_id,
            {
                "kind": "text",
                "uri": url,
                "filename": filename or f"{note_text[:12]}…",
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

    url = await put_object(media_type, file.filename or "upload", content_b64) if file is not None else ""

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
                id=f"bd-{uuid.uuid4().hex[:10]}",
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


def _build_text_chunks(note_text: str, title: str) -> list[Chunk]:
    pieces = _split_text_chunks(note_text)
    note_id = f"txt-note-{uuid.uuid4().hex[:6]}"  # 同一次上传共享虚拟音频组，便于证据面板归组
    chunks: list[Chunk] = []
    for i, piece in enumerate(pieces, start=1):
        chunks.append(
            Chunk(
                id=f"txt-{uuid.uuid4().hex[:10]}",
                audio_id=note_id,
                start="00:00",
                end="00:00",
                text=piece.replace("\n", " "),
                board_index=None,
                board_caption=f"文字笔记 · {title} 第{i}段",
                exam_point=f"笔记要点 {i}",
                difficulty=score(piece),
            )
        )
    return chunks


@router.get("/assets")
async def assets(session_id: str) -> list[dict]:
    return await repo.list_assets(session_id)
