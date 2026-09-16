# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""复习卷批改流水线：上传卷子 → 拆题 → 考点匹配定版解法 → 批改 → 生成变式新卷。

流程（两期规划的第一期全量 + 第二期的过程比对模块 + Rubric 分步采分）：
  1. OCR（图片卷）或直接文本 → paper_splitter 拆题
  2. 每题 match_from_text 向量匹配考点 → 检索老师定版解法（review 模式）
  3. 有学生过程 → 三级判分（见下方"判分层级"）
  4. 客观题 grade_objective 先判对错 → 每题生成同考点同难度变式新卷

判分层级（逐级回落，越靠前越可靠）
--------------------------------
  ① 确定性判分 `grade_objective_by_canonical`：结论词/末位数值与定版直接比对，
     不依赖 LLM，能判就判。
  ② **Rubric 分步采分** `rubric.rubric_grade`：拆解采分点 → 逐点比对 →
     按点给分 + 错因归因。这是理科阅卷的正确口径：方法对、中间算错一步
     应拿 7 分而不是 0 分。分数算术在 Python 侧完成，模型只做语义判定。
  ③ 整体判分 `diff_process`：Rubric 不可用时（无定版解法 / LLM 故障）的
     兜底，只回答"对不对、第一处偏离在哪"。

三级都判不出来时 `correct=None` → 计入 `needs_review` 交老师复核；
**绝不因为批改器故障把学生判成错**。
"""
from __future__ import annotations

import base64

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

import app.db.relational as repo
from app.api.v1.auth import AuthUser, current_user_optional
from app.core.tenancy import resolve_tenant
from app.services.agent.process_diff import diff_process, socratic_followup
from app.services.agent.quiz_generator import QuizGenerator, grade_objective
from app.services.agent.rubric import (
    DEFAULT_FULL_SCORE,
    rubric_attribution,
    rubric_followup,
    rubric_grade,
)
from app.services.extractor.difficulty import score
from app.services.extractor.exam_point import match_from_text
from app.services.extractor.paper_splitter import ExamQuestion, split_paper
from app.services.rag.retriever import get_retriever
from app.services.vlm.ocr_engine import OCREngine

router = APIRouter()

MAX_QUESTIONS = 12  # 单卷批改上限（演示规模，防止 LLM 费用失控）
# 每题满分。真实场景应由卷面/配置给定，此处统一按 10 分制——
# Rubric 会把模型拆出的采分点**归一化**到这个满分，避免"11/10 分"。
FULL_SCORE = DEFAULT_FULL_SCORE


@router.post("/exam/grade")
async def grade_exam(
    user: AuthUser = Depends(current_user_optional),
    session_id: str = Form(...),
    course_id: str = Form(""),
    file: UploadFile | None = File(None),
    text_content: str = Form(""),
    subject: str = Form("数学"),
):
    """上传复习卷（图片走 OCR / 文本直传），返回批改报告 + 同考点变式新卷。"""
    # 会话归属校验：与 chat/ingest 一致，防止跨用户提交到他人会话
    if not user.anonymous:
        owner = await repo.get_session_owner(session_id)
        if owner and owner != user.id:
            raise HTTPException(status_code=403, detail="无权向该会话提交试卷")
    tenant = resolve_tenant(user)

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
        point = await match_from_text(
            q.question_text, course_id=course_id or None, tenant_id=tenant
        )
        q.exam_point = point.name
        # 定版解法参照（review 模式：时间平权、canonical 保留）
        canonical_hits = await retriever.retrieve(
            q.question_text, top_k=2, course_id=course_id or None,
            retrieval_mode="review", with_context_window=False,
            tenant_id=tenant,
        )
        canonical = [c for c in canonical_hits if c.exam_point == point.name] or canonical_hits[:1]
        q.canonical_chunk_ids = [c.id for c in canonical]

        # ---- 三级判分：确定性 → Rubric 分步采分 → 整体判分 ----
        correct: bool | None = None
        attribution = ""
        followup: str | None = None
        diff_dict: dict | None = None
        rubric_dict: dict | None = None
        if q.student_answer:
            correct, attribution = grade_objective_by_canonical(q, canonical)
            if correct is None:
                rres = await rubric_grade(
                    q.student_answer, q.question_text, canonical, full_score=FULL_SCORE
                )
                if not rres.degraded:
                    rubric_dict = rres.to_dict()
                    correct = rres.final_answer_correct
                    attribution = rubric_attribution(rres) or rres.summary
                    followup = rubric_followup(rres)
                else:
                    # Rubric 不可用 → 退回整体判分（其 deviated 结果仍是有效信息）
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
            "rubric": rubric_dict,
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

    # 错因归因回填：优先用 Rubric 的根因类型（可聚合统计），
    # 没有分步结果时退到归因文本。错题本据此做"计算失误"vs"概念不清"的区分补救。
    pitfalls = [
        (r.get("rubric") or {}).get("error_label")
        or r["attribution"]
        for r in report if r["correct"] is False
    ]
    await repo.add_asset(session_id, {
        "kind": "exam",
        "uri": "",
        "filename": (file.filename if file else "") or f"复习卷·{len(questions)}题",
        "chunk_count": len(questions),
        "pitfalls": [p for p in pitfalls if p][:3],
    })

    scored = [r["rubric"] for r in report if r.get("rubric")]
    earned = sum(s["score"] for s in scored)
    possible = sum(s["full_score"] for s in scored)
    # 错因分布：运营端看的是"计算错误占比"这种可聚合信号，
    # 而不是一叠自由文本评语。
    dist: dict[str, int] = {}
    for s in scored:
        key = s.get("error_type") or "none"
        dist[key] = dist.get(key, 0) + 1

    return {
        "summary": {
            "total": len(report),
            "answered": len([r for r in report if r["student_answer"]]),
            "correct": len([r for r in report if r["correct"] is True]),
            "wrong": len([r for r in report if r["correct"] is False]),
            "needs_review": len([r for r in report if r["correct"] is None and r["student_answer"]]),
            # 分步采分统计：只有走通 Rubric 的题才计入，避免用
            # "未批改题=0 分" 拉低得分率（那会把批改器故障算成学生失分）。
            "graded_by_rubric": len(scored),
            "score_earned": round(earned, 2),
            "score_possible": round(possible, 2),
            "score_rate": round(earned / possible, 3) if possible else None,
            "error_distribution": dist,
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
