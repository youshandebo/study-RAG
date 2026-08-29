# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""复习卷拆题：把整页卷子的 OCR 文本按题号切分为独立题目。

规则版正则切分（阿拉伯题号 / 中文题号 / 括号题号），识别题干内
"解：/答：" 后的学生作答；切分效果不足时可由 LLM 兜底（预留）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class ExamQuestion:
    index: int                       # 卷面题号
    question_text: str               # 题干（含小问）
    student_answer: str | None = None  # 卷面上学生已写的作答（"解：/答："之后）
    exam_point: str | None = None      # 匹配到的老师考点（后续填充）
    canonical_chunk_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "question_text": self.question_text,
            "student_answer": self.student_answer,
            "exam_point": self.exam_point,
            "canonical_chunk_ids": self.canonical_chunk_ids,
        }


# 题号模式：1. / 1、 / 1． / (3) / （3） / 一、 / 12.
_NUM_HEADING = re.compile(
    r"(?:^|\n)\s*(\d{1,2}|[一二三四五六七八九十]{1,3})\s*[.、．)）]\s*",
)
# 学生作答标记
_ANSWER_MARK = re.compile(r"(?:^|\n)\s*(?:解|答|证明|解法)\s*[:：]\s*", re.MULTILINE)
_MIN_QUESTION_LEN = 6


def split_paper(ocr_text: str) -> list[ExamQuestion]:
    """按题号切分 OCR 文本。返回至少包含一段的题目列表（无法识别题号时整体作一题）。"""
    text = (ocr_text or "").strip()
    if not text:
        return []

    headings = [
        (m.start(1), m.group(1))
        for m in _NUM_HEADING.finditer(text)
    ]
    # 过滤伪题号：标题位置过密（如目录）或紧跟超长行首的误切，这里用
    # "相邻题号间隔文本长度 ≥ 6" 的启发式保留真实分题点
    cuts: list[tuple[int, str]] = []
    for pos, label in headings:
        if cuts and pos - cuts[-1][0] < _MIN_QUESTION_LEN:
            continue
        cuts.append((pos, label))

    questions: list[ExamQuestion] = []
    if len(cuts) < 2:
        # 无明显题号结构：整卷作一道题（index=1）
        questions.append(_build_question(1, text))
        return [q for q in questions if q.question_text]

    for i, (pos, label) in enumerate(cuts):
        end = cuts[i + 1][0] if i + 1 < len(cuts) else len(text)
        body = text[pos:end].strip()
        try:
            idx = int(label)
        except ValueError:
            idx = i + 1  # 中文题号按顺序编号
        q = _build_question(idx, body)
        if q.question_text:
            questions.append(q)

    # 切出过多（>30）说明正则误伤，退回整卷模式
    if len(questions) > 30:
        return [_build_question(1, text)]
    return questions


def _build_question(index: int, body: str) -> ExamQuestion:
    """剥离题号前缀，并把 '解：…' 之后的内容识别为学生作答。"""
    body = re.sub(r"^\s*\d{1,2}\s*[.、．)）]\s*", "", body)
    body = re.sub(r"^\s*[一二三四五六七八九十]{1,3}\s*[、.]\s*", "", body)
    body = body.strip()
    student_answer: str | None = None
    m = _ANSWER_MARK.search(body)
    if m:
        question_text = body[: m.start()].strip()
        student_answer = body[m.end() :].strip() or None
        # 作答标记前如果题干为空（纯解答题卷面），整段视作题干+作答混合
        if not question_text:
            question_text = body.strip()
            student_answer = None
    else:
        question_text = body
    return ExamQuestion(index=index, question_text=question_text, student_answer=student_answer)
