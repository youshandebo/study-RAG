# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""苏格拉底式启发伴学代理：分步设问状态机 + 学生作答评估。

真实模型可用时引导问题由 LLM 围绕学生当前所学现场生成（主题来自课堂
检索切片与对话历史）；无 Key / 生成失败时回落内置演示脚本，离线可演示。
"""
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

SOCRATIC_SYSTEM = (
    "你是苏格拉底式助教。绝不直接给出答案或完整解法，每次只提出一个引导性问题，"
    "把学生往推理的下一步推。用中文，不超过 80 字，数学公式用 $...$ 包裹。"
    "只输出问题本身，不要编号、不要前言。"
)


class SocraticTutor:
    async def start_or_advance(
        self,
        session_id: str,
        student_reply: str | None = None,
        *,
        topic: str = "",
        context: str = "",
        history: list[dict] | None = None,
    ) -> SocraticPayload:
        state = await repo.get_tutor_state(session_id)
        step_index = state.get("step_index", -1)
        history_log: list[dict] = state.get("history", [])

        if student_reply is not None and step_index >= 0:
            acknowledged = self._acknowledge(step_index, student_reply)
            history_log.append({"step": step_index, "reply": student_reply, "ok": bool(acknowledged)})
        else:
            acknowledged = ""

        total = len(SOCRATIC_STEPS)
        next_index = step_index + 1

        if next_index >= total:
            await repo.save_tutor_state(
                session_id,
                {"step_index": total, "finished": True, "history": history_log, "updated_at": time.time()},
            )
            return SocraticPayload(
                current_step_index=total,
                total_steps=total,
                guiding_question=SOCRATIC_CLOSING,
                hints=[],
            )

        # 真实模型：围绕学生正在学的内容现场生成下一问；失败回落演示脚本
        llm_question = await self._llm_question(
            topic=topic, context=context, chat_history=history or [],
            student_reply=student_reply, step_index=next_index, total=total,
        )
        base_question = llm_question or SOCRATIC_STEPS[next_index]
        question = (acknowledged + "\n\n" if acknowledged else "") + base_question
        hints = SOCRATIC_HINTS[: min(len(SOCRATIC_HINTS), next_index + 1)]

        await repo.save_tutor_state(
            session_id,
            {"step_index": next_index, "finished": False, "history": history_log, "updated_at": time.time()},
        )
        return SocraticPayload(
            current_step_index=next_index,
            total_steps=total,
            guiding_question=question,
            hints=hints,
        )

    @staticmethod
    async def _llm_question(
        *,
        topic: str,
        context: str,
        chat_history: list[dict],
        student_reply: str | None,
        step_index: int,
        total: int,
    ) -> str | None:
        """LLM 现场生成下一引导问题；演示引擎 / 异常返回 None 走演示脚本。"""
        try:
            from app.services.llm.provider import get_provider

            provider = get_provider()
            if provider.name == "mock-engine":
                return None
            hist_text = "\n".join(
                f"{'学生' if m.get('role') == 'user' else '助教'}: {str(m.get('content') or '')[:300]}"
                for m in chat_history[-4:]
            )
            parts = [f"学生正在学习的主题：{topic or '最近的课堂内容'}。"]
            if context:
                parts.append(f"相关课堂切片：\n{context[:800]}")
            if hist_text:
                parts.append(f"最近的对话：\n{hist_text}")
            if student_reply:
                parts.append(f"学生刚才的回答：{student_reply[:300]}")
            parts.append(f"现在进行第 {step_index + 1}/{total} 步引导，请只输出下一个引导性问题。")
            reply = (await provider.complete(
                [{"role": "user", "content": "\n".join(parts)}], system=SOCRATIC_SYSTEM,
            )).strip()
            return reply or None
        except Exception:
            return None

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
