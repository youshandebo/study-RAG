# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""核心考点自动提炼：规则信号 + LLM 双通道，演示模式直接映射课堂语料标注。"""
from __future__ import annotations

from dataclasses import dataclass

from app.services.llm.mock_engine import SOLVE_STEPS
from app.services.rag.chunker import Chunk

_POINT_SIGNALS = {
    "p-反常积分比较审敛法": ("抓大头", "比阶", "比较审敛"),
    "p-积分敛散性判定定理": ("p-积分", "口诀", "标准形"),
    "对数项比阶放缩技巧": ("对数", "放缩", "变式"),
    "瑕积分判别": ("瑕点", "瑕积分", "下限"),
}


@dataclass
class ExamPoint:
    name: str
    source_chunk_ids: list[str]
    summary: str = ""


def extract_from_chunks(chunks: list[Chunk]) -> list[ExamPoint]:
    points: list[ExamPoint] = []
    for name, signals in _POINT_SIGNALS.items():
        hits = [c.id for c in chunks if any(s in c.text for s in signals)]
        if hits:
            points.append(ExamPoint(name=name, source_chunk_ids=hits))
    return points


def extract_for_solution(problem_text: str) -> ExamPoint:
    lowered = problem_text or ""
    for name, signals in _POINT_SIGNALS.items():
        if any(s in lowered for s in signals):
            return ExamPoint(name=name, source_chunk_ids=[])
    return ExamPoint(name="p-反常积分比较审敛法", source_chunk_ids=[], summary="；".join(SOLVE_STEPS[:2]))


async def match_from_text(text: str, course_id: str | None = None, top: int = 1) -> ExamPoint:
    """通用考点匹配：对已入库切片做向量+BM25 混合检索，取最相关切片的考点标注。

    取代旧的关键词表硬编码——新学科/新课程入库即自动可匹配，无需改代码。
    检索不可用或无命中时回落关键词规则。
    """
    if not (text or "").strip():
        return ExamPoint(name="未识别考点", source_chunk_ids=[])
    try:
        from app.services.rag.retriever import get_retriever

        retriever = await get_retriever()
        hits = await retriever.retrieve_scored(
            text, top_k=top, course_id=course_id or None, retrieval_mode="review"
        )
        if hits:
            best = hits[0][1]
            return ExamPoint(
                name=best.exam_point or "未识别考点",
                source_chunk_ids=[c.id for _, c in hits],
                summary=best.text[:120],
            )
    except Exception:
        pass
    return extract_for_solution(text)
