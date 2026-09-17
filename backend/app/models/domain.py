# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""多态消息协议定义 —— 与前端 types/message.ts 严格一一对应。"""
from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class MessageType(str, Enum):
    solve_card = "solve_card"
    socratic_card = "socratic_card"
    quiz_card = "quiz_card"
    parallel_compare = "parallel_compare"
    exam_report_card = "exam_report_card"
    general_text = "general_text"


class Intent(str, Enum):
    solve = "solve"
    socratic = "socratic"
    quiz = "quiz"
    compare = "compare"
    general = "general"


class EvidenceRef(BaseModel):
    audio_id: str
    timestamp_range: tuple[str, str]
    audio_snippet_url: str
    board_image_url: str
    transcript_snippet: str = ""
    board_caption: str = ""


class SolvePayload(BaseModel):
    exam_point: str
    difficulty: int = Field(ge=1, le=5)
    pitfalls: list[str] = []
    steps: list[str] = []
    evidence_list: list[EvidenceRef] = []


class SocraticSelfTest(BaseModel):
    """CONVERGING 阶段挂起的自测题（结构化下发，供前端渲染成可点选项）。

    为什么要把选项结构化成字段、而不是只用 `guiding_question` 里的文本：
    文本形态要求学生手敲一整段作答，判分规则命中率低、体验也差。
    结构化下发给前端渲染"点选项即提交"，才能让确定性判分真正吃到规律。
    """

    question_text: str = ""
    options: list[str] = []


class SocraticPayload(BaseModel):
    current_step_index: int
    total_steps: int
    guiding_question: str
    hints: list[str] = []
    selftest: Optional[SocraticSelfTest] = None
    # P2-A 显式状态机载荷：阶段与提示阶梯对前端可见（可选，保持向后兼容）。
    # 前端据此显隐"提示进度""已揭晓"等，但真正的流转由服务端 FSM 决定。
    phase: str = ""
    phase_label: str = ""
    hint_level: int = 0
    max_hint_level: int = 3
    revealed: bool = False
    guard_blocked: bool = False
    converge_failed: bool = False


class QuizPayload(BaseModel):
    question_text: str
    options: Optional[list[str]] = None
    target_pitfall: str
    explanation: str
    answer: Optional[str] = None        # 正确答案（批改用，前端选择性展示）
    difficulty: int = 3                 # 1-5


class CompareTrack(BaseModel):
    model_name: str
    content: str = ""
    status: Literal["streaming", "done"] = "streaming"


class ComparePayload(BaseModel):
    tracks: list[CompareTrack]


class RubricStepView(BaseModel):
    """单个采分点的前端渲染契约（与 services/agent/rubric.py 的 to_dict 对齐）。"""

    no: int = 0
    name: str = ""
    points: float = 0.0
    hit: str = ""            # hit / partial / miss
    awarded: float = 0.0
    evidence: str = ""
    error_type: Optional[str] = None
    follow_through: bool = False


class RubricView(BaseModel):
    full_score: float = 0.0
    score: float = 0.0
    ratio: float = 0.0
    steps: list[RubricStepView] = []
    final_answer_correct: Optional[bool] = None
    first_missed: Optional[int] = None
    root_cause_step: Optional[int] = None
    affected_steps: list[int] = []
    error_type: Optional[str] = None
    error_label: str = ""
    summary: str = ""
    degraded: bool = False
    degraded_reason: str = ""


class ExamQuestionView(BaseModel):
    index: int = 0
    question_text: str = ""
    student_answer: Optional[str] = None
    exam_point: Optional[str] = None
    difficulty: int = 3
    correct: Optional[bool] = None
    attribution: str = ""
    socratic_followup: Optional[str] = None
    rubric: Optional[RubricView] = None


class ExamSummaryView(BaseModel):
    total: int = 0
    answered: int = 0
    correct: int = 0
    wrong: int = 0
    needs_review: int = 0
    graded_by_rubric: int = 0
    score_earned: float = 0.0
    score_possible: float = 0.0
    score_rate: Optional[float] = None
    error_distribution: dict[str, int] = {}


class ExamVariantView(BaseModel):
    """单道变式题（字段口径与 QuizGenerator.generate_variant 的 QuizPayload 对齐）。

    变式题直接复用 quiz 卡的渲染口径（question_text / options / answer），
    前端可以拿同一套"选项即答"组件渲染，而不是再造一套题型协议。
    """

    for_question: int = 0
    exam_point: str = ""
    question_text: str = ""
    options: Optional[list[str]] = None
    answer: Optional[str] = None
    target_pitfall: str = ""
    explanation: str = ""
    difficulty: int = 3


class ExamReportPayload(BaseModel):
    """复习卷批改报告卡片载荷。

    为什么走消息管道而不是独立页面：学生的学习轨迹发生在对话流里——
    批改结果必须与提问、讲解出现在同一条时间线上，学生才会回看；
    独立报告页的跳出率在真实产品里高得可怜。同时它随消息持久化，
    换设备/跨会话刷新后仍然可见（Dexie hydrate 照常工作）。
    """

    summary: ExamSummaryView = Field(default_factory=ExamSummaryView)
    questions: list[ExamQuestionView] = []
    variants: list[ExamVariantView] = []


class UsageInfo(BaseModel):
    """单轮 Token 用量（估算口径，供 Cherry Studio 式展示与上下文容量条）。"""
    input: int = 0
    output: int = 0
    total: int = 0
    duration_ms: int = 0
    cache_hit_rate: float = 0.0
    context_used: int = 0
    context_limit: int = 131072
    # [{label: 消息|系统提示词|检索上下文|回复输出|其他, tokens: int}]
    context_breakdown: list[dict[str, Any]] = []


class PolymorphicMessage(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    session_id: str
    role: Literal["user", "assistant", "system"]
    type: MessageType
    content: str = ""
    created_at: int = Field(default_factory=lambda: int(time.time() * 1000))
    intent: Optional[Intent] = None

    solve_payload: Optional[SolvePayload] = None
    socratic_payload: Optional[SocraticPayload] = None
    quiz_payload: Optional[QuizPayload] = None
    compare_payload: Optional[ComparePayload] = None
    exam_report_payload: Optional[ExamReportPayload] = None
    usage: Optional[UsageInfo] = None

    def to_client(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        return data


class ChatRequest(BaseModel):
    session_id: str
    text: str = ""
    image_b64: Optional[str] = None
    force_intent: Optional[Intent] = None
    course_id: str = ""      # 课程作用域：限定检索范围（空=全库）
    chapter: str = ""        # 章节软过滤（输入框 Chip 指定；空=不限章节）
    retrieval_mode: str = "lecture"  # lecture/review_narrow/review_broad/review/explore
    time_alpha_override: float | None = None   # 时间偏好强度覆盖（优先于模式默认）
    canonical_bonus_override: float | None = None  # 定版权重覆盖
    request_id: str = ""     # 客户端幂等键：同 id 重复提交不重复扣额度（空=不启用幂等）


class IngestRequest(BaseModel):
    session_id: str
    filename: str
    media_type: Literal["audio", "board", "text"]
    content_b64: str = ""
    lecture_date: str = ""
