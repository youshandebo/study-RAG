"""基于易错点规则的靶向自测出题器。"""
from __future__ import annotations

from app.models.domain import QuizPayload
from app.services.llm.mock_engine import (
    QUIZ_EXPLANATION,
    QUIZ_OPTIONS,
    QUIZ_QUESTION,
    QUIZ_TARGET_PITFALL,
)


class QuizGenerator:
    def generate(self, target_pitfall: str | None = None) -> QuizPayload:
        pitfall = target_pitfall or QUIZ_TARGET_PITFALL
        return QuizPayload(
            question_text=QUIZ_QUESTION,
            options=list(QUIZ_OPTIONS),
            target_pitfall=pitfall,
            explanation=QUIZ_EXPLANATION,
        )
