"""异步入库任务：上传 -> ASR -> VLM -> 洞察提炼 -> 向量入库。

Celery 可用时走分布式队列；否则同步执行（单机演示模式）。
"""
from __future__ import annotations

from app.core.celery_app import celery_app


def run_ingest_pipeline(session_id: str, media_type: str, raw: bytes, filename: str = "") -> dict:
    """流水线同步实现（演示/单机模式直接调用）。"""
    import asyncio

    async def _inner() -> dict:
        from app.db.minio_client import put_object
        from app.db import relational as repo
        from app.services.asr.hotwords import correct
        from app.services.asr.transcriber import Transcriber
        from app.services.extractor.difficulty import score
        from app.services.rag.aligner import align_boards
        from app.services.rag.chunker import chunk_transcript
        from app.services.rag.retriever import get_retriever

        import base64

        content_b64 = base64.b64encode(raw).decode()
        url = await put_object(media_type, filename or "upload", content_b64)

        if media_type != "audio":
            return {"uri": url, "note": "board assets handled via ingest API"}

        segments = await Transcriber().transcribe(raw, filename)
        for seg in segments:
            seg.text = correct(seg.text)
        chunks = chunk_transcript(f"task-{abs(hash(filename)) % 10_000}", segments)
        chunks = align_boards(chunks, board_count=12)
        for c in chunks:
            c.difficulty = score(c.text)

        retriever = await get_retriever()
        # Embedding 写入向量库(Qdrant/内存)后，register_chunks 内部会同步增量更新
        # 内存 BM25 倒排索引（读写锁保护，写独占/读共享，避免并发检索读到半成品索引）
        added = await retriever.register_chunks(chunks)
        await repo.add_asset(session_id, {"kind": media_type, "uri": url, "filename": filename, "chunk_count": added})
        return {"uri": url, "chunks_added": added}

    return asyncio.run(_inner())


if celery_app is not None:  # pragma: no cover - 仅在安装 celery+redis 时注册

    @celery_app.task(name="ingest.pipeline")
    def ingest_task(session_id: str, media_type: str, raw_b64: str, filename: str = "") -> dict:
        import base64

        return run_ingest_pipeline(session_id, media_type, base64.b64decode(raw_b64), filename)
