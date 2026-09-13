# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""课程大纲树聚合端点测试：层级、统计量、租户与课程隔离、空值归桶。"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services.rag.chunker import Chunk
from app.services.rag.retriever import get_retriever


def _chunk(cid: str, audio: str, course: str, chapter: str, point: str, canonical: bool = False) -> Chunk:
    return Chunk(
        id=cid, audio_id=audio, start="00:00", end="00:10", text=f"文本 {cid}",
        course_id=course, chapter=chapter, exam_point=point, is_canonical=canonical,
    )


@pytest.mark.asyncio
async def test_outline_aggregates_three_levels():
    retriever = await get_retriever()
    await retriever.register_chunks([
        _chunk("o1", "lec01", "math101", "第一节", "夹逼准则", canonical=True),
        _chunk("o2", "lec01", "math101", "第一节", "夹逼准则"),
        _chunk("o3", "lec01", "math101", "第一节", "ε-δ 定义"),
        _chunk("o4", "lec02", "math101", "第二节", "洛必达法则"),
        _chunk("o5", "lec02", "other-course", "第二节", "别的课程"),
    ])

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        resp = await ac.get("/api/v1/course/math101/outline")
    assert resp.status_code == 200
    data = resp.json()
    assert data["course_id"] == "math101"

    lectures = {l["lecture_id"]: l for l in data["lectures"]}
    assert set(lectures) >= {"lec01", "lec02"}
    lec01 = lectures["lec01"]
    assert lec01["chunk_count"] == 3
    ch = next(c for c in lec01["chapters"] if c["name"] == "第一节")
    pts = {p["name"]: p for p in ch["exam_points"]}
    assert pts["夹逼准则"]["chunk_count"] == 2
    assert pts["夹逼准则"]["has_canonical"] is True
    assert pts["ε-δ 定义"]["has_canonical"] is False
    # 别的课程的切片不得混入
    assert all("别的课程" not in p["name"] for l in data["lectures"] for c in l["chapters"] for p in c["exam_points"])


@pytest.mark.asyncio
async def test_outline_buckets_unlabeled_metadata():
    retriever = await get_retriever()
    await retriever.register_chunks([
        _chunk("u1", "lecX", "phy201", "", ""),
    ])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        resp = await ac.get("/api/v1/course/phy201/outline")
    assert resp.status_code == 200
    lec = next(l for l in resp.json()["lectures"] if l["lecture_id"] == "lecX")
    assert lec["chapters"][0]["name"] == "未分章节"
    assert lec["chapters"][0]["exam_points"][0]["name"] == "未标注考点"


@pytest.mark.asyncio
async def test_outline_empty_course_returns_empty_lectures():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        resp = await ac.get("/api/v1/course/no-such-course/outline")
    assert resp.status_code == 200
    assert resp.json() == {"course_id": "no-such-course", "lectures": []}
