# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""复习卷批改流水线：上传卷子 → 拆题 → 考点匹配定版解法 → 批改 → 生成变式新卷。

流程（两期规划的第一期全量 + 第二期的过程比对模块）：
  1. OCR（图片卷）或直接文本 → paper_splitter 拆题
  2. 每题 match_from_text 向量匹配考点 → 检索老师定版解法（review 模式）
  3. 有学生过程 → process_diff 逐步比对（LLM 不可用自动降级整体判分）
  4. 客观题 grade_objective 先判对错 → 每题生成同考点同难度变式新卷
"""
from __future__ import annotations

import base64

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

import app.db.relational as repo
from app.services.agent.process_diff import diff_process, socratic_followup
from app.services.agent.quiz_generator import QuizGenerator, grade_objective
from app.services.extractor.difficulty import score
from app.services.extractor.exam_point import match_from_text
from app.services.extractor.paper_splitter import ExamQuestion, split_paper
from app.services.rag.retriever import get_retriever
from app.services.vlm.ocr_engine import OCREngine

router = APIRouter()

MAX_QUESTIONS = 12  # 单卷批改上限（演示规模，防止 LLM 费用失控）


@router.post("/exam/grade")
async def grade_exam(
    session_id: str = Form(...),
    course_id: str = Form(""),
    file: UploadFile | None = File(None),
    text_content: str = Form(""),
    subject: str = Form("数学"),
):
    """上传复习卷（图片走 OCR / 文本直传），返回批改报告 + 同考点变式新卷。"""
    raw = await file.read() if file is not None else b""
    ocr_text = (text_content or "").strip()
    if not ocr_text and raw:
        if media_is_image(file):
            content_b64 = base64.b64encode(raw).decode()
            ocr = await OCREngine().recognize(content_b64)
            ocr_text = ocr.get("problem_text") or ""
        else:
            try:
                ocr_text = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise HTTPException(status_code=400, detail="文本卷需为 UTF-8 编码") from None
    if not ocr_text:
        raise HTTPException(status_code=400, detail="卷面内容为空")

    questions = split_paper(ocr_text)[:MAX_QUESTIONS]
    if not questions:
        raise HTTPException(status_code=422, detail="未能从卷面识别出任何题目")

    retriever = await get_retriever()
    generator = QuizGenerator()

    report: list[dict] = []
    new_paper: list[dict] = []

    for q in questions:
        # 考点匹配（向量检索，通用化，不再依赖关键词表）
        point = await match_from_text(q.question_text, course_id=course_id or None)
        q.exam_point = point.name
        # 定版解法参照（review 模式：时间平权、canonical 保留）
        canonical_hits = await retriever.retrieve(
            q.question_text, top_k=2, course_id=course_id or None,
            retrieval_mode="review", with_context_window=False,
        )
        canonical = [c for c in canonical_hits if c.exam_point == point.name] or canonical_hits[:1]
        q.canonical_chunk_ids = [c.id for c in canonical]

        # 批改：客观题先精确判分；有学生过程再做步骤比对（可降级）
        correct: bool | None = None
        attribution = ""
        followup: str | None = None
        diff_dict: dict | None = None
        if q.student_answer:
            correct, attribution = grade_objective_by_canonical(q, canonical)
            if correct is None:
                diff = await diff_process(q.student_answer, q.question_text, canonical)
                diff_dict = diff.to_dict()
                correct = diff.is_final_answer_correct
                attribution = diff.deviation_desc or attribution
                followup = socratic_followup(diff)
        else:
            attribution = "卷面未作答"

        level = score(q.question_text)
        report.append({
            **q.to_dict(),
            "difficulty": level,
            "correct": correct,
            "attribution": attribution,
            "socratic_followup": followup,
            "process_diff": diff_dict,
        })

        # 变式新卷：同考点同难度，优先覆盖本次暴露的薄弱点
        variant = await generator.generate_variant(
            exam_point=point.name,
            difficulty=level,
            avoid_pitfall=(q.student_answer and attribution) or None,
        )
        new_paper.append({
            "for_question": q.index,
            "exam_point": point.name,
            **variant.model_dump(),
        })

    await repo.add_asset(session_id, {
        "kind": "exam",
        "uri": "",
        "filename": (file.filename if file else "") or f"复习卷·{len(questions)}题",
        "chunk_count": len(questions),
        "pitfalls": [r["attribution"] for r in report if r["correct"] is False][:3],
    })

    return {
        "summary": {
            "total": len(report),
            "answered": len([r for r in report if r["student_answer"]]),
            "correct": len([r for r in report if r["correct"] is True]),
            "wrong": len([r for r in report if r["correct"] is False]),
            "needs_review": len([r for r in report if r["correct"] is None and r["student_answer"]]),
        },
        "report": report,
        "new_paper": new_paper,
    }


def media_is_image(file: UploadFile | None) -> bool:
    return bool(file and (file.content_type or "").startswith("image/"))


def _conclusion(text: str) -> str | None:
    """提取敛散性结论词（"发散"优先于"收敛"，防"收敛性"误匹配）。"""
    if "发散" in text:
        return "发散"
    if "收敛" in text:
        return "收敛"
    return None


def grade_objective_by_canonical(q: ExamQuestion, canonical: list) -> tuple[bool | None, str]:
    """对照定版解法文本的粗判分：结论词比对 + 末尾数值/选项字母比对。

    粗判不出结果时返回 None，交给 process_diff（LLM）或老师复核。
    """
    import re

    if not q.student_answer or not canonical:
        return None, ""
    canon_text = canonical[0].text
    # 结论词：学生与定版都给出明确结论时直接判定对错
    stu_c, canon_c = _conclusion(q.student_answer), _conclusion(canon_text)
    if stu_c and canon_c:
        if stu_c == canon_c:
            return True, f"结论「{stu_c}」与老师定版一致"
        return (
            False,
            f"你的结论是「{stu_c}」，但按老师定版解法应为「{canon_c}」"
            f"（可回顾考点「{canonical[0].exam_point}」的推导）",
        )
    # 末尾数值/选项字母比对
    stu_finals = re.findall(r"([A-D]|[+-]?\d+(?:\.\d+)?(?:/\d+)?)\s*$", q.student_answer.strip())
    if not stu_finals:
        return None, ""
    stu = stu_finals[-1]
    nums = re.findall(r"=\s*([+-]?\d+(?:/\d+)?)", canon_text)
    for cand in nums:
        if stu == cand or stu == cand.replace("/", "÷"):
            return True, "结论与老师定版一致"
    if re.fullmatch(r"[A-D]", stu):
        return None, ""  # 选择题字母无定版对照 → 交给 LLM/复核
    return None, ""
