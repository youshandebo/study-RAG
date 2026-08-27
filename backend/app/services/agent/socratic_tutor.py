"""苏格拉底式启发伴学代理：以有限状态机推进分步设问，支持学生作答评估与渐进提示。"""
from __future__ import annotations

import time

import app.db.relational as repo
from app.models.domain import SocraticPayload
from app.services.extractor import difficulty
from app.services.llm.mock_engine import (
    SOCRATIC_CLOSING,
    SOCRATIC_HINTS,
    SOCRATIC_STEPS,
)

AFFIRM_WORDS = ("是", "对", "x2", "x^2", "平方", "大头", "多项式", "幂函数")


class SocraticTutor:
    async def start_or_advance(self, session_id: str, student_reply: str | None = None) -> SocraticPayload:
        state = await repo.get_tutor_state(session_id)
        step_index = state.get("step_index", -1)
        history: list[dict] = state.get("history", [])

        if student_reply is not None and step_index >= 0:
            acknowledged = self._acknowledge(step_index, student_reply)
            history.append({"step": step_index, "reply": student_reply, "ok": bool(acknowledged)})
        else:
            acknowledged = ""

        total = len(SOCRATIC_STEPS)
        next_index = step_index + 1

        if next_index >= total:
            await repo.save_tutor_state(
                session_id,
                {"step_index": total, "finished": True, "history": history, "updated_at": time.time()},
            )
            return SocraticPayload(
                current_step_index=total,
                total_steps=total,
                guiding_question=SOCRATIC_CLOSING,
                hints=[],
            )

        question = (acknowledged + "\n\n" if acknowledged else "") + SOCRATIC_STEPS[next_index]
        hints = SOCRATIC_HINTS[: min(len(SOCRATIC_HINTS), next_index + 1)]

        await repo.save_tutor_state(
            session_id,
            {"step_index": next_index, "finished": False, "history": history, "updated_at": time.time()},
        )
        return SocraticPayload(
            current_step_index=next_index,
            total_steps=total,
            guiding_question=question,
            hints=hints,
        )

    @staticmethod
    def _acknowledge(step_index: int, reply: str) -> str:
        hit = any(w in reply.lower() or w in reply for w in AFFIRM_WORDS)
        prefix = f"你说「{reply.strip()[:40]}」——"
        if step_index == 0:
            return (
                prefix + ("判断准确！$x^2$ 确实是增长的大头。" if hit else "方向值得鼓励，再想想哪一项阶数更高？")
            )
        if step_index in (1, 2):
            return prefix + ("很好，化简正在逼近标准形。" if hit else "试着动手除一下，保留最高阶项。")
        return prefix + ("不错，继续往下推。" if hit else "没关系，回看上一条的提示再试一次。")

    @staticmethod
    def mastery_delta(total_steps: int, finished: bool) -> int:
        base = difficulty.score("".join(SOCRATIC_STEPS)) * 3
        return base + (10 if finished else 0)
