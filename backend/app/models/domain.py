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


class SocraticPayload(BaseModel):
    current_step_index: int
    total_steps: int
    guiding_question: str
    hints: list[str] = []


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
