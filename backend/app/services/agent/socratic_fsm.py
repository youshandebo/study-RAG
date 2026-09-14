# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""苏格拉底引导的**显式状态机**（FSM）——不是 Prompt 约束，而是代码级的流转控制。

为什么必须是状态机，而不是"在 system prompt 里写一句不要直接给答案"
--------------------------------------------------------------------
纯提示词工程版的"苏格拉底"有一个致命退化：多轮之后，学生在反复催促
（"你到底会不会""直接告诉我答案""别废话"）下，模型几乎必然破防泄题。
因为是否给答案这件事，**由模型的临场发挥决定**，而不是由一段可审计、
可测试、可回放的逻辑决定。一旦泄题，也没有任何状态可供复盘。

本模块把"当前处于哪个阶段、脚手架升到第几级、能不能揭晓答案"全部
变成**显式、可持久化、纯函数驱动**的状态。LLM 只负责在该阶段的约束下
组织语言，**无权决定阶段流转**。

三段式取舍
----------
1. `detect_signal` 只做**粗判**（关键词/形态），判错也不会致命——它驱动的
   是"问得更细一点"还是"换个角度"，不涉及对错结论。
2. 自测题的对错**不靠启发式**，而由 `graded=` 传入客观判分结果
   （`quiz_generator.grade_objective` 的口径）。对话信号是启发式，
   判分信号是确定性的，两者不混用。
3. 揭晓答案（REVEALED）是**硬门槛**：必须同时满足"提示已到最高级"
   且"确实反复卡住"。逃逸（ESCAPE）永远不能直接开启揭晓。

本模块零外部依赖、零 IO、零随机——因此可以被完整单元测试覆盖。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SocraticPhase(str, Enum):
    """会话的显式阶段。持久化到 DB，跨请求、跨副本可续。"""

    diagnosing = "diagnosing"   # 诊断：摸清学生卡在哪、误在哪
    guiding = "guiding"         # 引导：梯级提示 / 脚手架
    reflecting = "reflecting"   # 反思：诱导学生复述与自我验证
    converging = "converging"   # 收敛：出题自测，独立复现
    resolved = "resolved"       # 终结：已掌握
    revealed = "revealed"       # 终结：揭晓答案（仅最高提示级 + 仍卡住）


# 终结态：吸收态，任何信号都不再改变阶段
TERMINAL_PHASES = frozenset({SocraticPhase.resolved, SocraticPhase.revealed})

# 提示阶梯上限。级别语义见 HINT_LADDER。
MAX_HINT_LEVEL = 3

# 达到最高提示级后，还需"仍然卡住"多少次才允许揭晓。
# 设成 2 而不是 1：给"最后一级提示其实已经点醒他、只是还没组织好语言"
# 留一次机会，避免学生刚看一眼提示就被直接喂答案。
STUCK_THRESHOLD = 2

# 提示阶梯的语义标签（1 概念点拨 / 2 局部残卷 / 3 关键分支）。
# 只做"点到为止"的层级划分，绝不包含任何可直接抄走的完整答案。
HINT_LADDER: dict[int, str] = {
    0: "未开启脚手架",
    1: "概念点拨",
    2: "局部残卷",
    3: "关键分支解析",
}

PHASE_LABEL: dict[SocraticPhase, str] = {
    SocraticPhase.diagnosing: "诊断误区",
    SocraticPhase.guiding: "梯级引导",
    SocraticPhase.reflecting: "诱导反思",
    SocraticPhase.converging: "收敛验证",
    SocraticPhase.resolved: "已掌握",
    SocraticPhase.revealed: "答案揭晓",
}


class Signal(str, Enum):
    """一次学生回复（或一次判分）被归类出的信号。"""

    none = "none"         # 无回复 / 无法归类（含首次发起）
    attempt = "attempt"   # 有尝试，但未到位
    correct = "correct"   # 有实质推进（启发式认定）
    stuck = "stuck"       # 明确卡住 / 主动求提示
    escape = "escape"     # 绕过引导意图（要求直接给答案）→ 硬拦截
    wrong = "wrong"       # 自测判分：答错（确定性信号）
    passed = "passed"     # 自测判分：答对（确定性信号）


# --------------------------------------------------------------- 逃逸守卫 ----
# 越狱意图词表。命中即 ESCAPE——**优先级高于一切其他信号**：
# 一句"别废话，直接告诉我答案，是不是 x^2"里既有催促也有答案猜测，
# 若先按 attempt 处理，就等于让越狱措辞顺带把脚手架升级了。
_ESCAPE_PATTERNS = (
    "直接告诉我答案", "直接给答案", "直接告诉我", "告诉我答案", "给我答案", "给我答案吧",
    "答案是什么", "答案是啥", "答案呢", "结果是什么", "直接给结果", "直接说答案",
    "别废话", "少废话", "不要废话", "不要引导", "不用引导", "别引导", "别问了", "不要问了",
    "抄答案", "直接说", "我不要提示", "不要提示",
    "just tell me", "tell me the answer", "give me the answer", "answer please",
    "skip the hints", "stop asking", "no hints", "just give",
)

_STUCK_PATTERNS = (
    "不会", "不懂", "不明白", "不知道", "想不出来", "想不到", "没思路", "没有思路",
    "卡住", "卡了", "太难", "不会做", "不知道怎么做", "不理解", "晕了", "放弃了",
    "stuck", "i don't know", "i dont know", "no idea", "help me",
)

# 有"尝试痕迹"的形态：公式、等号、数字、以及表达推断的口吻词。
_ATTEMPT_MARKERS = (
    "我觉得", "我认为", "我算", "我猜", "应该是", "是不是", "大概", "可能", "也许",
    "因为", "所以", "先", "然后", "代入", "求导", "积分", "极限", "收敛", "发散",
    "i think", "maybe", "because", "so ",
)
_ATTEMPT_RE = re.compile(r"(\$|=|\^|\d|[a-zA-Z]\s*[+\-*/])")

# 正向确认词（需与"尝试痕迹"同时出现才算推进，避免一句"对"就被判定掌握）
_AFFIRM_PATTERNS = (
    "对", "是的", "没错", "正确", "对了", "嗯", "有道理", "懂了", "明白了", "原来如此",
    "yes", "right", "got it", "correct",
)

_PUNCT_RE = re.compile(r"[\s,，。.!！?？、;；:：\"'“”‘’()（）\[\]【】]")


def _norm(text: str) -> str:
    """归一化：去空白与常见标点，转小写。用于词表匹配。"""
    return _PUNCT_RE.sub("", (text or "").lower())


def is_escape(text: str | None) -> bool:
    """是否命中"绕过引导、直接要答案"的越狱意图。

    单独暴露成函数：一是便于测试，二是调用方可以在不推进状态机的前提下
    先做一次拦截（例如首次发起就带着"直接给答案"）。
    """
    normalized = _norm(text or "")
    if not normalized:
        return False
    return any(_norm(p) in normalized for p in _ESCAPE_PATTERNS)


def detect_signal(reply: str | None, *, graded: bool | None = None) -> Signal:
    """把一次学生回复归类为 `Signal`。

    `graded` 用于自测题路径：True=答对 / False=答错，是**确定性**信号，
    优先于一切文本启发式（判分结果比措辞可靠）。其余情况走文本粗判。

    判定优先级（顺序即优先级）：
        逃逸 > 判分 > 有尝试(正确/推进) > 卡住 > 空
    """
    if graded is True:
        return Signal.passed
    if graded is False:
        return Signal.wrong

    text = (reply or "").strip()
    if not text:
        return Signal.none

    if is_escape(text):
        return Signal.escape

    normalized = _norm(text)
    has_attempt = any(_norm(m) in normalized for m in _ATTEMPT_MARKERS) or bool(_ATTEMPT_RE.search(text))
    has_affirm = any(_norm(a) in normalized for a in _AFFIRM_PATTERNS)

    if has_attempt:
        return Signal.correct if has_affirm else Signal.attempt
    if any(_norm(s) in normalized for s in _STUCK_PATTERNS):
        return Signal.stuck
    if has_affirm:
        # 光说"对"但没有任何推理内容：无法确认掌握，按"有尝试"处理
        return Signal.attempt
    return Signal.none


# ------------------------------------------------------------- 状态对象 ----
@dataclass
class SocraticState:
    """一个会话的 FSM 状态。`to_dict` / `from_dict` 用于持久化。"""

    phase: SocraticPhase = SocraticPhase.diagnosing
    hint_level: int = 0             # 0..MAX_HINT_LEVEL
    turns_in_phase: int = 0         # 当前阶段内已进行的轮次（含本轮）
    stuck_count: int = 0            # 在最高提示级下"仍然卡住"的累计次数
    escape_attempts: int = 0        # 越狱尝试次数（可观测/可风控）
    question_count: int = 0         # 累计已发起的引导轮次
    converge_failed: bool = False   # 收敛阶段自测失败过 → 错题本归档信号
    guard_blocked: bool = False     # 本轮是否被逃逸守卫拦截
    last_signal: str = Signal.none.value
    history: list[dict[str, Any]] = field(default_factory=list)

    # ---------------------------------------------------------- 判定 ----
    @property
    def is_terminal(self) -> bool:
        return self.phase in TERMINAL_PHASES

    @property
    def can_reveal(self) -> bool:
        """是否已满足"揭晓答案"的硬门槛（最高提示级 + 反复卡住）。"""
        return (
            self.phase in (SocraticPhase.guiding, SocraticPhase.reflecting)
            and self.hint_level >= MAX_HINT_LEVEL
            and self.stuck_count >= STUCK_THRESHOLD
        )

    # -------------------------------------------------------- 序列化 ----
    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase.value,
            "hint_level": int(self.hint_level),
            "turns_in_phase": int(self.turns_in_phase),
            "stuck_count": int(self.stuck_count),
            "escape_attempts": int(self.escape_attempts),
            "question_count": int(self.question_count),
            "converge_failed": bool(self.converge_failed),
            "guard_blocked": bool(self.guard_blocked),
            "last_signal": self.last_signal,
            "history": list(self.history),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> SocraticState:
        """从持久化字典还原；脏数据（未知阶段等）一律安全回落初始态。"""
        d = dict(data or {})
        try:
            phase = SocraticPhase(str(d.get("phase") or SocraticPhase.diagnosing.value))
        except ValueError:
            phase = SocraticPhase.diagnosing
        try:
            hint_level = max(0, min(MAX_HINT_LEVEL, int(d.get("hint_level") or 0)))
        except (TypeError, ValueError):
            hint_level = 0
        history = d.get("history")
        if not isinstance(history, list):
            history = []
        return cls(
            phase=phase,
            hint_level=hint_level,
            turns_in_phase=max(0, int(d.get("turns_in_phase") or 0)),
            stuck_count=max(0, int(d.get("stuck_count") or 0)),
            escape_attempts=max(0, int(d.get("escape_attempts") or 0)),
            question_count=max(0, int(d.get("question_count") or 0)),
            converge_failed=bool(d.get("converge_failed")),
            guard_blocked=bool(d.get("guard_blocked")),
            last_signal=str(d.get("last_signal") or Signal.none.value),
            history=history,
        )


def _settle(state: SocraticState, new_phase: SocraticPhase) -> SocraticState:
    """落到新阶段：进入新阶段则轮次归 1，否则当前阶段轮次 +1。

    轮次口径必须写在**一处**——否则"留在阶段里"和"换阶段"的计数会各自
    漂移，最后没人说得清 turns_in_phase 到底代表什么。
    """
    if new_phase is not state.phase:
        state.turns_in_phase = 1
    else:
        state.turns_in_phase += 1
    state.phase = new_phase
    state.question_count += 1
    return state


def advance(state: SocraticState, signal: Signal) -> SocraticState:
    """纯函数状态流转：`(旧状态, 信号) -> 新状态`（不修改入参）。

    这是整个苏格拉底引擎的**唯一**流转入口。LLM 无权改写状态——
    它只能拿到 `advance` 之后的阶段与提示级去组织语言。
    """
    s = SocraticState.from_dict(state.to_dict())  # 深拷贝，保持纯函数语义
    s.guard_blocked = False
    s.last_signal = signal.value

    # 终结态吸收：已掌握 / 已揭晓后不再流转
    if s.is_terminal:
        s.question_count += 1
        return s

    # --- 逃逸守卫：最高优先级，任何阶段都硬拦截，绝不进入揭晓 ---
    if signal is Signal.escape:
        s.escape_attempts += 1
        s.guard_blocked = True
        # 平滑回退到"轻度引导"：不因越狱措辞而升级脚手架
        if s.phase is SocraticPhase.diagnosing:
            new_phase = SocraticPhase.diagnosing
        else:
            new_phase = SocraticPhase.guiding
        if s.hint_level > 1:
            s.hint_level = 1
        elif s.hint_level == 0 and new_phase is SocraticPhase.guiding:
            s.hint_level = 1
        return _settle(s, new_phase)

    # --- 正常阶段流转 ---
    new_phase = s.phase

    if s.phase is SocraticPhase.diagnosing:
        if signal in (Signal.correct, Signal.passed):
            new_phase = SocraticPhase.reflecting
        elif signal in (Signal.attempt, Signal.stuck, Signal.wrong):
            new_phase = SocraticPhase.guiding
            s.hint_level = max(1, s.hint_level)
        # none：继续诊断

    elif s.phase is SocraticPhase.guiding:
        if signal in (Signal.correct, Signal.passed):
            new_phase = SocraticPhase.reflecting
        elif signal is Signal.stuck:
            s.stuck_count += 1
            s.hint_level = min(MAX_HINT_LEVEL, s.hint_level + 1)
            # 硬门槛：最高提示级 + 反复卡住，才允许揭晓
            if s.hint_level >= MAX_HINT_LEVEL and s.stuck_count >= STUCK_THRESHOLD:
                new_phase = SocraticPhase.revealed
        elif signal in (Signal.attempt, Signal.wrong):
            # 每轮未到位的尝试/答错 → 脚手架升一级（1→2→3）
            s.stuck_count += 1 if signal is Signal.wrong else 0
            s.hint_level = min(MAX_HINT_LEVEL, s.hint_level + 1)
        # none：留在当前级继续等

    elif s.phase is SocraticPhase.reflecting:
        if signal in (Signal.correct, Signal.passed):
            new_phase = SocraticPhase.converging
        elif signal in (Signal.wrong, Signal.stuck):
            # 反思阶段答错/卡住 = 反思没到位 → 退回引导补脚手架
            s.stuck_count += 1 if signal is Signal.stuck else 0
            new_phase = SocraticPhase.guiding
            s.hint_level = max(1, s.hint_level)
        # attempt / none：留在反思，继续追问

    elif s.phase is SocraticPhase.converging:
        if signal in (Signal.passed, Signal.correct):
            new_phase = SocraticPhase.resolved
        elif signal in (Signal.wrong, Signal.stuck):
            # 自测失败：标记错题（供错题本归档），退回引导重新搭脚手架
            s.converge_failed = True
            s.stuck_count = 0
            s.hint_level = 1
            new_phase = SocraticPhase.guiding
        # attempt / none：留在收敛，等学生作答

    return _settle(s, new_phase)


def phase_system_prompt(phase: SocraticPhase, hint_level: int) -> str:
    """按当前阶段 + 提示级生成**给 LLM 的系统约束**。

    注意：这只是"语言组织"的约束，阶段本身由 `advance` 决定。
    即使模型无视这段提示，也无法让状态机跳级或提前揭晓。
    """
    ladder = HINT_LADDER.get(max(0, min(MAX_HINT_LEVEL, int(hint_level))), "")
    common = (
        "你是苏格拉底式助教。用中文，不超过 80 字，数学公式用 $...$ 包裹。"
        "只输出你要说的话，不要编号、不要前言。"
    )
    if phase is SocraticPhase.diagnosing:
        body = "当前阶段【诊断误区】：先别答疑，用一个问题摸清学生卡在哪一步、误在哪。"
    elif phase is SocraticPhase.guiding:
        body = (
            f"当前阶段【梯级引导】，提示级别 {hint_level}/{MAX_HINT_LEVEL}（{ladder}）。"
            "每次只推进一小步，只给阶梯式提示，绝不给出完整答案或完整解法。"
        )
    elif phase is SocraticPhase.reflecting:
        body = "当前阶段【诱导反思】：请学生用自己的话复述思路、或验证某一步，不要代他总结。"
    elif phase is SocraticPhase.converging:
        body = "当前阶段【收敛验证】：请学生独立完成最后一步 / 出一道同型小题让他自测。"
    elif phase is SocraticPhase.revealed:
        body = "当前阶段【答案揭晓】：学生已在最高提示级仍反复卡住，现在可以给出完整解法与关键步骤。"
    else:  # resolved
        body = "当前阶段【已掌握】：肯定学生，并提示可以做一道变式巩固。"
    return f"{common}{body}"


def hint_label(hint_level: int) -> str:
    return HINT_LADDER.get(max(0, min(MAX_HINT_LEVEL, int(hint_level))), "")


def phase_label(phase: SocraticPhase) -> str:
    return PHASE_LABEL.get(phase, phase.value)
