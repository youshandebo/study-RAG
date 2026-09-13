# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""课程大纲树聚合端点：把切片元数据聚成 Lecture → Chapter → ExamPoint 三级导航。

设计要点
--------
- **零新表**：直接对 retriever.all_chunks 的存量元数据做应用层聚合，
  切片入库/定版变更后下一次请求即反映，无需物化刷新。
- **租户硬隔离**：all_chunks(tenant_id=...) 已在数据层收窄，本端点只做课程过滤，
  不存在跨租户串数据的路径。
- 层级口径：lecture = audio_id（一次课件/录音上传），chapter = chapter 字段，
  exam_point = exam_point 字段（空值归入「未标注」桶，不丢弃）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.v1.auth import AuthUser, current_user_optional
from app.core.tenancy import resolve_tenant

router = APIRouter()

_UNLABELED_CHAPTER = "未分章节"
_UNLABELED_POINT = "未标注考点"


@router.get("/course/{course_id}/outline")
async def course_outline(course_id: str, user: AuthUser = Depends(current_user_optional)):
    """课程大纲树：讲座 → 章节 → 考点（含切片数与定版标记）。

    前端大纲树数据源；点考点即可按 chapter 软过滤定向答疑。
    """
    from app.services.rag.retriever import get_retriever

    tenant = resolve_tenant(user)
    retriever = await get_retriever()
    chunks = await retriever.all_chunks(tenant_id=tenant)

    # lecture_id -> {title, chapters: {chapter -> {exam_point -> stats}}}
    lectures: dict[str, dict] = {}
    for c in chunks:
        if c.course_id != course_id:
            continue
        lec = lectures.setdefault(
            c.audio_id,
            {"lecture_id": c.audio_id, "title": c.audio_id, "chapters": {}},
        )
        chapter = (c.chapter or "").strip() or _UNLABELED_CHAPTER
        ch = lec["chapters"].setdefault(chapter, {})
        point = (c.exam_point or "").strip() or _UNLABELED_POINT
        st = ch.setdefault(point, {"chunk_count": 0, "has_canonical": False})
        st["chunk_count"] += 1
        if c.is_canonical:
            st["has_canonical"] = True

    out_lectures = []
    for lec in lectures.values():
        chapters = [
            {
                "name": ch_name,
                "exam_points": [
                    {"name": pt_name, **stats}
                    for pt_name, stats in sorted(
                        ch_pts.items(), key=lambda kv: (-kv[1]["chunk_count"], kv[0])
                    )
                ],
            }
            for ch_name, ch_pts in sorted(lec["chapters"].items())
        ]
        out_lectures.append(
            {
                "lecture_id": lec["lecture_id"],
                "title": lec["title"],
                "chapters": chapters,
                "chunk_count": sum(p["chunk_count"] for c in chapters for p in c["exam_points"]),
            }
        )
    out_lectures.sort(key=lambda l: l["lecture_id"])

    return {"course_id": course_id, "lectures": out_lectures}
