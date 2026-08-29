# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""靶向出题器：LLM 依据考点/难度/薄弱点真实生成，演示引擎兜底。

- generate：围绕易错陷阱出一道即时自测题（原接口，行为升级）
- generate_variant：批改流水线用——同考点同难度出变式题，
  优先覆盖学生刚暴露的薄弱点
"""
from __future__ import annotations

from app.models.domain import QuizPayload
from app.services.extractor.difficulty import score
from app.services.llm.json_call import ask_llm_json
from app.services.llm.mock_engine import (
    QUIZ_EXPLANATION,
    QUIZ_OPTIONS,
    QUIZ_QUESTION,
    QUIZ_TARGET_PITFALL,
)

_SYSTEM = (
    "你是大学数学课堂的出题助教。只输出一个 JSON 对象，不要任何多余文字。"
    "字段：question_text（题干，LaTeX 公式用 $...$ 包裹）、options（四个选项字符串数组，"
    "解答/填空题可为 null）、answer（正确答案：与选项之一一致，或最终结果表达式）、"
    "target_pitfall（本题考查的易错点，一句话）、explanation（解析 ≤120 字，引用课堂方法）。"
)


def _valid_quiz(data: dict | None) -> QuizPayload | None:
    if not data or not str(data.get("question_text") or "").strip():
        return None
    options = data.get("options")
    if options is not None and (not isinstance(options, list) or len(options) < 2):
        options = None
    try:
        level = max(1, min(5, int(data.get("difficulty") or 3)))
    except (TypeError, ValueError):
        level = 3
    return QuizPayload(
        question_text=str(data["question_text"]),
        options=[str(o) for o in options] if options else None,
        target_pitfall=str(data.get("target_pitfall") or "综合考查"),
        explanation=str(data.get("explanation") or "解析生成失败，请参考课堂切片复习。"),
        answer=str(data["answer"]) if data.get("answer") is not None else None,
        difficulty=level,
    )


def _mock_quiz(target_pitfall: str | None) -> QuizPayload:
    return QuizPayload(
        question_text=QUIZ_QUESTION,
        options=list(QUIZ_OPTIONS),
        target_pitfall=target_pitfall or QUIZ_TARGET_PITFALL,
        explanation=QUIZ_EXPLANATION,
        answer=QUIZ_OPTIONS[0],
        difficulty=score(QUIZ_QUESTION),
    )


class QuizGenerator:
    async def generate(self, target_pitfall: str | None = None) -> QuizPayload:
        """即时自测：LLM 围绕易错点出单选题；不可用时回落演示题。"""
        pitfall = target_pitfall or QUIZ_TARGET_PITFALL
        prompt = (
            f"请围绕易错点「{pitfall}」出一道大学数学单选题，"
            "干扰项须来自学生的典型错误思路。输出 JSON。"
        )
        return _valid_quiz(await ask_llm_json(_SYSTEM, prompt)) or _mock_quiz(pitfall)

    async def generate_variant(
        self,
        exam_point: str,
        difficulty: int | None = None,
        avoid_pitfall: str | None = None,
    ) -> QuizPayload:
        """批改流水线第二步：同考点同难度变式，优先覆盖学生刚暴露的薄弱点。"""
        level = max(1, min(5, difficulty or 3))
        prompt = (
            f"围绕考点「{exam_point}」出一道难度等级 {level}/5 的变式训练题。"
            + (f"学生刚在「{avoid_pitfall}」上出错，本题必须针对该薄弱点设问。" if avoid_pitfall else "")
            + "题型可选选择题或填空题（填空题 options 为 null）。输出 JSON。"
        )
        payload = _valid_quiz(await ask_llm_json(_SYSTEM, prompt))
        if payload is not None:
            payload.difficulty = level
            if avoid_pitfall and payload.target_pitfall == "综合考查":
                payload.target_pitfall = avoid_pitfall
            return payload
        # 演示兜底：模板化变式（结构完整，可离线演示）
        fallback = _mock_quiz(avoid_pitfall)
        fallback.question_text = (
            f"【变式 · {exam_point}】结合课堂讲过的方法，判断相关问题的敛散性并写出关键步骤。"
            f"（难度 {level}/5）"
        )
        fallback.options = None
        fallback.difficulty = level
        return fallback


def grade_objective(payload: QuizPayload, student_answer: str | None) -> tuple[bool | None, str]:
    """客观题判分：与 payload.answer 比对（选项字母或文本等价）。

    无标准答案 / 学生未作答时返回 None（需 AI 步骤批改或老师复核）。
    """
    if not payload.answer:
        return None, "本题无标准答案，需结合解析复核"
    if not (student_answer or "").strip():
        return None, "学生未作答"
    stu = student_answer.strip()
    ans = payload.answer.strip()
    options = payload.options or []
    alias = {chr(ord("A") + i): str(opt) for i, opt in enumerate(options)}
    stu_norm = alias.get(stu.upper(), stu)
    ans_norm = alias.get(ans.upper(), ans)
    correct = stu_norm == ans_norm or stu.upper() == ans.upper()
    return correct, ("回答正确" if correct else f"正确答案：{ans}")
