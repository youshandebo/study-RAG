# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""苏格拉底式启发伴学代理：**显式状态机驱动**的分步引导。

职责边界（重要）
----------------
本类只负责"把 FSM 决定的阶段翻译成人话"：
    读状态 → `detect_signal` 归类学生回复 → `advance` 流转 → 按阶段组织提问 → 落库。
**阶段流转与"能否揭晓答案"完全由 `socratic_fsm` 决定，LLM 无权改写。**
这样即便模型在用户反复催促下"心软"，也无法越过状态机提前泄题——
泄题与否是代码逻辑，不是模型的临场发挥。

真实模型可用时，引导问题由 LLM 在**当前阶段的约束**下现场生成
（主题来自课堂检索切片与对话历史）；无 Key / 生成失败时回落内置演示脚本，离线可演示。
"""
from __future__ import annotations

from app.models.domain import SocraticPayload
from app.services.agent import socratic_store
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

# 被逃逸守卫拦截时给学生的柔性说明——不是训斥，而是"换个方式继续帮你"
_GUARD_NOTICE = "我先不直接把答案给你——难题拆开就不难了。我们只看一个小地方："

# 演示脚本的最大步数：与前端进度条口径一致
TOTAL_STEPS = len(SOCRATIC_STEPS)


def _mock_question(state: SocraticState) -> str:
    """无真实模型时的兜底脚本：按已发起轮次顺序推进内置讲法。"""
    if state.phase is SocraticPhase.revealed or state.phase is SocraticPhase.resolved:
        return SOCRATIC_CLOSING
    idx = min(max(0, state.question_count), TOTAL_STEPS - 1)
    return SOCRATIC_STEPS[idx]


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
        graded: bool | None = None,
    ) -> SocraticPayload:
        """读状态 → 归类信号 → 流转 → 组织提问 → 落库，返回本轮引导载荷。

        `graded`：自测题路径的客观判分结果（True 答对 / False 答错）。
        传入时优先于文本启发式——判分结果永远比措辞可靠。
        """
        stored = await socratic_store.load_state(session_id)
        state = SocraticState.from_dict(stored)

        # 首轮：用户输入是"主题/开场白"，不是对某个引导问题的作答，
        # 不做信号归类（否则"教教我"里的"教"都可能被误判）。诊断阶段会
        # 用一个澄清式提问反问他卡在哪，方向依然正确。
        is_first_turn = stored is None
        if is_first_turn or student_reply is None:
            signal = Signal.none
        else:
            signal = detect_signal(student_reply, graded=graded)
        state = advance(state, signal)

        # 事件日志（截断保留，避免无限增长）——供后续错题本/教研分析回溯
        if student_reply is not None:
            state.history.append({
                "n": state.question_count,
                "signal": signal.value,
                "phase": state.phase.value,
                "hint_level": state.hint_level,
                "reply": (student_reply or "")[:200],
            })
            state.history = state.history[-50:]

        question = await self._question_for(
            state, topic=topic, context=context, history=history or [], student_reply=student_reply,
        )
        if state.guard_blocked:
            question = f"{_GUARD_NOTICE}\n\n{question}"

        await socratic_store.save_state(
            session_id, state,
            tenant_id=tenant_id, user_id=user_id, course_id=course_id, concept_tag=concept_tag,
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

    async def _question_for(
        self,
        state: SocraticState,
        *,
        topic: str,
        context: str,
        history: list[dict],
        student_reply: str | None,
    ) -> str:
        """按当前阶段组织提问：LLM 现场生成优先，失败回落演示脚本。"""
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
        *,
        state: SocraticState,
        topic: str,
        context: str,
        chat_history: list[dict],
        student_reply: str | None,
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
