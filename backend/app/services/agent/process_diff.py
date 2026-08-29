# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""逐步骤过程比对：学生作答 vs 老师定版解法，定位第一处偏离步骤。

第二期能力：不直接判"错了"，而是产出苏格拉底式反问——先问学生该步的
思考依据，学生答后再对照老师原法做精准归因。LLM 不可用时安全降级为
"整体对错 + 待老师复核"。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.services.llm.json_call import ask_llm_json
from app.services.rag.chunker import Chunk

_SYSTEM = (
    "你是大学数学课堂的批改助教。对比老师的标准解法与学生的作答过程，"
    "只输出一个 JSON 对象："
    '{"first_deviation_step": 整数（学生第一处偏离标准解法的步骤序号，从 1 开始；'
    '完全一致或无法分步时为 null），'
    '"deviation_desc": 一句话描述偏离点（学生当时做了什么/漏了什么），'
    '"is_final_answer_correct": 布尔值（最终结果是否正确），'
    '"followup_question": 苏格拉底式反问（针对偏离点问学生的思考依据，不直接说错）。'
    "语气参照课堂助教，温和且引用老师的方法名。"
)


@dataclass
class ProcessDiffResult:
    first_deviation_step: int | None = None
    deviation_desc: str = ""
    is_final_answer_correct: bool | None = None
    followup_question: str = ""
    degraded: bool = False          # True = LLM 不可用，降级为整体判分
    socratic_hints: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "first_deviation_step": self.first_deviation_step,
            "deviation_desc": self.deviation_desc,
            "is_final_answer_correct": self.is_final_answer_correct,
            "followup_question": self.followup_question,
            "degraded": self.degraded,
            "socratic_hints": self.socratic_hints,
        }


def _canonical_text(chunks: list[Chunk]) -> str:
    return "\n---\n".join(
        f"[{c.exam_point}·{c.start}-{c.end}] {c.text}" for c in chunks
    )[:3000]


def _fallback(student_answer: str | None, reason: str) -> ProcessDiffResult:
    return ProcessDiffResult(
        deviation_desc=f"（{reason}，已跳过逐步比对，请老师复核过程）",
        is_final_answer_correct=None,
        followup_question="",
        degraded=True,
        socratic_hints=[],
    )


async def diff_process(
    student_answer: str | None,
    question_text: str,
    canonical_chunks: list[Chunk],
) -> ProcessDiffResult:
    """LLM 逐步比对。学生未作答或无定版参照时直接降级。"""
    if not (student_answer or "").strip():
        return _fallback(student_answer, "学生未写过程")
    if not canonical_chunks:
        return _fallback(student_answer, "该考点暂无老师定版解法参照")

    prompt = (
        f"【题目】\n{question_text}\n\n"
        f"【老师标准解法（课堂定版）】\n{_canonical_text(canonical_chunks)}\n\n"
        f"【学生作答过程】\n{student_answer[:2500]}\n\n"
        "请逐步比对并输出 JSON。"
    )
    data = await ask_llm_json(_SYSTEM, prompt, max_tokens=800)
    if not data:
        return _fallback(student_answer, "AI 批改引擎暂不可用")

    step = data.get("first_deviation_step")
    try:
        step = int(step) if step is not None else None
    except (TypeError, ValueError):
        step = None
    correct = data.get("is_final_answer_correct")
    hints = []
    if step:
        hints.append(f"回顾第 {step} 步之前用到的知识点")
    if canonical_chunks:
        hints.append(f"对照课堂「{canonical_chunks[0].exam_point}」的标准推导再验一遍")
    return ProcessDiffResult(
        first_deviation_step=step,
        deviation_desc=str(data.get("deviation_desc") or "")[:300],
        is_final_answer_correct=bool(correct) if isinstance(correct, bool) else None,
        followup_question=str(data.get("followup_question") or "")[:300],
        degraded=False,
        socratic_hints=hints,
    )


def socratic_followup(diff: ProcessDiffResult) -> str | None:
    """有偏离点时生成反问（不复述答案），交由 socratic_tutor 状态机续聊。"""
    if diff.degraded or not diff.first_deviation_step:
        return None
    return (
        diff.followup_question
        or f"你在第 {diff.first_deviation_step} 步（{diff.deviation_desc}）是怎么想的？"
        "能说说你这一步的推导依据吗？"
    )
