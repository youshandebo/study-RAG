"""多模型并发派发与聚合接口：单 SSE 连接内并行推送多条模型轨道。"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.services.llm.dispatch import TrackDispatcher


class CompareBody(BaseModel):
    question: str
    model_keys: list[str] | None = None  # gpt | claude | qwen | deepseek


router = APIRouter()


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/compare/stream")
async def compare_stream(req: CompareBody) -> StreamingResponse:
    dispatcher = TrackDispatcher()
    track_defs = dispatcher.track_names(req.model_keys)

    async def gen():
        queue: asyncio.Queue = asyncio.Queue()

        async def pump(index: int, key: str, display: str) -> None:
            try:
                async for piece in dispatcher.stream(key, req.question):
                    await queue.put(("track_delta", {"index": index, "model_name": display, "text": piece}))
            except Exception:
                pass
            finally:
                await queue.put(("track_done", {"index": index, "model_name": display}))

        yield _sse("meta", {"tracks": [d for _, d in track_defs]})
        tasks = [asyncio.create_task(pump(i, k, d)) for i, (k, d) in enumerate(track_defs)]
        done = 0
        while done < len(tasks):
            event, data = await queue.get()
            if event == "track_done":
                done += 1
            yield _sse(event, data)
        for t in tasks:
            t.cancel()
        yield _sse("done", {})

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
