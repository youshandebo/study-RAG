# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""苏格拉底式启发伴学代理：**显式状态机驱动**的分步引导 + 收敛自测闭环。

职责边界（重要）
----------------
本类只负责"把 FSM 决定的阶段翻译成人话"，并串起自测闭环：
    读状态 → 归类信号（含对挂起自测题的判分）→ `advance` 流转
    → 按阶段组织提问 / 出收敛自测题 → 落库（状态 + 挂起题）
**阶段流转与"能否揭晓答案"完全由 `socratic_fsm` 决定，LLM 无权改写。**
这样即便模型在用户反复催促下"心软"，也无法越过状态机提前泄题。

CONVERGING 自测闭环（P2-B）
--------------------------
`REFLECTING` 判为掌握 → 进入 `CONVERGING` → 本题**出题并挂起**（落库）；
学生下一轮作答 → 先判分（确定性规则优先，开放题退到结构化）→ 回灌 FSM：
- 判对（`graded=True`）→ 吸收进 `RESOLVED`，清空挂起题
- 判错（`graded=False`）→ 打 `converge_failed`、回退 `GUIDING`，并**被动归档错题本**
- 判不出来（`None`）→ 留在 `CONVERGING`（既不冤枉也不放水，见 grading 模块）
"""
from __future__ import annotations

import logging

from app.models.domain import SocraticPayload
from app.services.agent import socratic_store
from app.services.agent.quiz_generator import QuizGenerator
from app.services.agent.socratic_fsm import (
    MAX_HINT_LEVEL,
    SocraticPhase,
    SocraticState,
    Signal,
    advance,
    detect_signal,
    hint_label,
    phase_label,
    phase_system_prompt,
)
from app.services.extractor import difficulty
from app.services.llm.mock_engine import (
    SOCRATIC_CLOSING,
    SOCRATIC_HINTS,
    SOCRATIC_STEPS,
)
from app.services.notebook import store as notebook_store
from app.services.notebook.grading import grade_with_fallback

_logger = logging.getLogger("app.services.agent.socratic_tutor")

# 被逃逸守卫拦截时给学生的柔性说明——不是训斥，而是"换个方式继续帮你"
_GUARD_NOTICE = "我先不直接把答案给你——难题拆开就不难了。我们只看一个小地方："

# 演示脚本的最大步数：与前端进度条口径一致
TOTAL_STEPS = len(SOCRATIC_STEPS)


def _mock_question(state: SocraticState) -> str:
    """无真实模型时的兜底脚本：按已发起轮次顺序推进内置讲法。"""
    if state.phase in (SocraticPhase.revealed, SocraticPhase.resolved):
        return SOCRATIC_CLOSING
    idx = min(max(0, state.question_count), TOTAL_STEPS - 1)
    return SOCRATIC_STEPS[idx]


def _format_selftest(quiz: dict) -> str:
    """把自测题渲染成给学生看的文本（题干 + 选项）。"""
    lines = ["我们来做一道小题检验一下：", str(quiz.get("question_text") or "")]
    options = quiz.get("options") or []
    if options:
        lines.append("\n".join(f"{chr(65 + i)}. {o}" for i, o in enumerate(options)))
    return "\n\n".join(line for line in lines if line)


class SocraticTutor:
    async def start_or_advance(
        self,
        session_id: str,
        student_reply: str | None = None,
        *,
        topic: str = "",
        context: str = "",
        history: list[dict] | None = None,
        tenant_id: str = "public",
        user_id: str = "",
        course_id: str = "",
        concept_tag: str = "",
        exam_point: str = "",
        graded: bool | None = None,
    ) -> SocraticPayload:
        """读状态 → 归类信号（可含自测判分）→ 流转 → 组织提问/出题 → 落库。"""
        stored = await socratic_store.load_state(session_id)
        state = SocraticState.from_dict(stored)
        pending = await socratic_store.get_pending_quiz(session_id)
        is_first_turn = stored is None

        # ------------------------------------------------ 信号归类 ----
        consumed_quiz: dict | None = None
        self_test_verdict: bool | None = None

        if is_first_turn or student_reply is None:
            # 首轮（或主动发起）：用户输入是"主题/开场白"，不做信号归类
            signal = Signal.none
        elif graded is not None:
            # 调用方已给出判分（如 /quiz/grade 回灌）
            signal = Signal.passed if graded else Signal.wrong
            self_test_verdict = graded
            consumed_quiz = pending
        elif pending:
            # 挂起着一道自测题 → 本轮作答先判分，再回灌状态机
            verdict, reason = await grade_with_fallback(
                reference_answer=pending.get("answer"),
                student_answer=student_reply,
                options=pending.get("options"),
                question=str(pending.get("question_text") or ""),
            )
            consumed_quiz = pending
            if verdict is True:
                signal, self_test_verdict = Signal.passed, True
            elif verdict is False:
                signal, self_test_verdict = Signal.wrong, False
            else:
                # 判不出来：留在 CONVERGING 继续问，不因判分器不可用而放行/惩罚
                signal = Signal.attempt
                _logger.info("自测判分未定（%s），保持收敛阶段", reason)
        else:
            signal = detect_signal(student_reply)

        state = advance(state, signal)

        # ------------------------------------------------ 事件日志 ----
        if student_reply is not None:
            state.history.append({
                "n": state.question_count,
                "signal": signal.value,
                "phase": state.phase.value,
                "hint_level": state.hint_level,
                "reply": (student_reply or "")[:200],
            })
            state.history = state.history[-50:]

        # 已作答的自测题不再挂起
        if consumed_quiz is not None:
            await socratic_store.set_pending_quiz(session_id, None)

        # --------------------------------- 被动归档：自测失败入错题本 ----
        if self_test_verdict is False and consumed_quiz:
            await self._archive_mistake(
                tenant_id=tenant_id, user_id=user_id, course_id=course_id,
                concept_tag=concept_tag or exam_point or topic,
                session_id=session_id, quiz=consumed_quiz,
            )

        # ---------------------------------------------- 组织本轮输出 ----
        question, _pending_after = await self._compose(
            state, session_id=session_id, topic=topic, context=context,
            history=history or [], student_reply=student_reply,
            exam_point=exam_point, still_pending=(pending if consumed_quiz is None else None),
        )

        if state.guard_blocked:
            question = f"{_GUARD_NOTICE}\n\n{question}"

        await socratic_store.save_state(
            session_id, state,
            tenant_id=tenant_id, user_id=user_id, course_id=course_id,
            concept_tag=concept_tag or exam_point,
        )

        terminal = state.phase in (SocraticPhase.resolved, SocraticPhase.revealed)
        step_index = TOTAL_STEPS if terminal else min(state.question_count, TOTAL_STEPS - 1)
        hints = (
            SOCRATIC_HINTS[: min(len(SOCRATIC_HINTS), max(1, state.hint_level))]
            if state.hint_level > 0 and not terminal else []
        )
        return SocraticPayload(
            current_step_index=step_index,
            total_steps=TOTAL_STEPS,
            guiding_question=question,
            hints=hints,
            phase=state.phase.value,
            phase_label=phase_label(state.phase),
            hint_level=state.hint_level,
            max_hint_level=MAX_HINT_LEVEL,
            revealed=state.phase is SocraticPhase.revealed,
            guard_blocked=state.guard_blocked,
            converge_failed=state.converge_failed,
        )

    # ------------------------------------------------------------ 内部 ----
    async def _compose(
        self,
        state: SocraticState,
        *,
        session_id: str,
        topic: str,
        context: str,
        history: list[dict],
        student_reply: str | None,
        exam_point: str,
        still_pending: dict | None,
    ) -> tuple[str, dict | None]:
        """按当前阶段产出要发给学生的话；CONVERGING 阶段负责出题/复用挂起题。"""
        if state.phase is SocraticPhase.converging:
            quiz = still_pending
            if quiz is None:
                payload = await QuizGenerator().generate(
                    target_pitfall=exam_point or topic or None,
                    exam_point=exam_point or topic,
                    context=context,
                )
                quiz = payload.model_dump()
                await socratic_store.set_pending_quiz(session_id, quiz)
            return _format_selftest(quiz), quiz
        return await self._question_for(
            state, topic=topic, context=context, history=history, student_reply=student_reply,
        ), None

    async def _archive_mistake(
        self, *, tenant_id: str, user_id: str, course_id: str,
        concept_tag: str, session_id: str, quiz: dict,
    ) -> None:
        """引导自测失败 → 被动归档错题本（含题目、参考答案、诊断误区）。

        写入失败只记日志：错题本必须能容错，绝不能因归档失败而让整轮引导报错。
        """
        try:
            await notebook_store.add_mistake(
                tenant_id=tenant_id, user_id=user_id, course_id=course_id,
                concept_tag=concept_tag or "未标注",
                source_type=notebook_store.SOURCE_PASSIVE,
                session_id=session_id, trace_id=session_id,
                question_context=str(quiz.get("question_text") or ""),
                reference_answer=str(quiz.get("answer") or ""),
                options=quiz.get("options") or None,
                misconception=str(quiz.get("target_pitfall") or quiz.get("explanation") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning("错题被动归档失败（不影响本轮引导）: %s", exc)

    async def _question_for(
        self, state: SocraticState, *,
        topic: str, context: str, history: list[dict], student_reply: str | None,
    ) -> str:
        """非收敛阶段：按阶段组织提问（LLM 现场生成优先，失败回落演示脚本）。"""
        llm_question = await self._llm_question(
            state=state, topic=topic, context=context,
            chat_history=history, student_reply=student_reply,
        )
        if llm_question:
            return llm_question
        base = _mock_question(state)
        if state.phase is SocraticPhase.revealed:
            return f"好，我们把完整解法过一遍（这一步你已经努力到位了）：\n\n{base}"
        return base

    @staticmethod
    async def _llm_question(
        *, state: SocraticState, topic: str, context: str,
        chat_history: list[dict], student_reply: str | None,
    ) -> str | None:
        """LLM 现场生成当前阶段的引导语；演示引擎 / 异常返回 None 走演示脚本。"""
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
            parts.append(
                f"当前引导阶段：{phase_label(state.phase)}；"
                f"提示级别：{state.hint_level}/{MAX_HINT_LEVEL}（{hint_label(state.hint_level)}）。"
                "请据此只输出这一轮你要说的话。"
            )
            reply = (
                await provider.complete(
                    [{"role": "user", "content": "\n".join(parts)}],
                    system=phase_system_prompt(state.phase, state.hint_level),
                )
            ).strip()
            return reply or None
        except Exception:  # noqa: BLE001 - 生成失败一律回落演示脚本，不打断引导
            return None

    @staticmethod
    def mastery_delta(state: SocraticState, base_text: str = "") -> int:
        """本次引导带来的掌握度增量（供前端画像使用）。

        到达终结态（已掌握/已揭晓）给满额；随提示级别递减——
        提示吃得越多，说明越依赖脚手架，掌握度增益应越小。
        """
        base = difficulty.score(base_text or "".join(SOCRATIC_STEPS)) * 3
        if state.phase is SocraticPhase.resolved:
            return base + 10
        return max(1, base - state.hint_level * 2)
