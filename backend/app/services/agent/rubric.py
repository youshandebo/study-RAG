# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""Rubric 细粒度分步采分：题干拆解 → 步骤匹配 → 采分点分步给分 → 错因归因。

为什么需要它（替代"一次性 Prompt 判对错"）
-----------------------------------------
`process_diff` 是**单次** LLM 调用：它只回答"第一处偏离在哪、最终答案对不对"。
这对"学生错在哪"很有用，但对理科阅卷远远不够——真实的理科给分是**按步骤给分**
的：一道 10 分题，方法对、中间算错一步，应该拿 7 分而不是 0 分。二进制的
"对/错"既不符合教学实际，也无法给学生可执行的改进方向（"你错了" ≠
"你在第 2 步的换元限代错了"）。

本模块把批改拆成四个阶段：
  1. 拆解：由老师定版解法反推采分点清单（每点有名目与分值）
  2. 匹配：逐点比对学生的作答过程，判 hit / partial / miss
  3. 给分：**算术在 Python 侧完成**（见下）
  4. 归因：把首个未命中的采分点映射为错误类型，回填错题本

三条刻意的职责边界
------------------
**1. 分数算术绝不交给模型。** 让 LLM 直接吐 `score: 7` 是最常见的错误做法：
   模型算术不可靠（尤其多步累加），更重要的是**不可审计**——老师和学生问
   "这 3 分扣在哪一步"时，一个黑箱数字答不上来，而分步明细能逐条对账。
   模型只输出语义判定（hit/partial/miss），分值由本模块按权重算出。

**2. 模型给的权重要归一化。** 拆解出的采分点分值之和常常不等于满分
   （拆细了变成 12 分、拆粗了只有 8 分）。直接累加会出现"11/10 分"这种
   荒谬结果，因此按满分等比例缩放到 `full_score`。

**3. follow-through（连锁给分）必须显式支持。** 理科步骤是链式的：第 2 步
   用的是第 1 步的结果。若学生第 1 步算错、但第 2 步**正确地沿用了自己的
   错误中间结果**，真实阅卷是给第 2 步分的。分步判分若不处理这点，会比整体
   判分更苛刻——一步错则全盘皆输，那反而是在惩罚写过程的学生。
   本模块让模型标注 `follow_through`，由调用方（老师复核界面）可见。

失败方向
--------
判分器不可用 / 输出非法 → `degraded=True`，由调用方回落到 `process_diff`
的整体判分。**绝不因为批改器异常就把学生判成错**——这是本项目一贯口径
（见 `notebook/grading.py`：判分不确定 ≠ 判错）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.services.llm.json_call import ask_llm_json
from app.services.rag.chunker import Chunk

_logger = logging.getLogger("app.services.agent.rubric")

DEFAULT_FULL_SCORE = 10.0

# partial（部分命中）的给分比例。取固定值而非让模型给数字——模型给出的
# 0.37 这类数字无法复现也无法向家长解释，固定档位才可审计。
PARTIAL_RATIO = 0.5

HIT = "hit"
PARTIAL = "partial"
MISS = "miss"
_VALID_HITS = (HIT, PARTIAL, MISS)

# 错因归因枚举。模型只能从这里选，否则归为 None 待复核——自由文本归因
# 无法聚合统计（运营端要看"计算错误占比"这种分布）。
ERROR_CONCEPT = "concept"        # 概念/知识性错误
ERROR_COMPUTATION = "computation"  # 计算错误（方法对、算术错）
ERROR_METHOD = "method"          # 方法选择错误
ERROR_OMISSION = "omission"      # 步骤跳跃/遗漏
_VALID_ERRORS = (ERROR_CONCEPT, ERROR_COMPUTATION, ERROR_METHOD, ERROR_OMISSION)

_ERROR_LABEL = {
    ERROR_CONCEPT: "概念理解有误",
    ERROR_COMPUTATION: "计算失误（方法正确）",
    ERROR_METHOD: "方法选择不当",
    ERROR_OMISSION: "步骤跳跃或遗漏",
}

_SYSTEM = (
    "你是大学数学课堂的阅卷助教，采用**按步骤给分**的评分标准（Rubric）。\n"
    "第一步：根据老师的标准解法，把这道题拆成若干**采分点**（每点一个明确的关键"
    "步骤或结论，按解题顺序排列，通常 3-6 个）。\n"
    "第二步：逐点比对学生的作答过程，判定命中情况。\n\n"
    "只输出一个 JSON 对象：\n"
    '{"full_score": 本题满分数字（未说明时按 10 分），\n'
    ' "steps": [{"no": 序号（从1开始）, "name": 采分点名目（10字内）, '
    '"points": 该点分值数字, "hit": "hit"|"partial"|"miss", '
    '"evidence": 学生作答中对应的一句原文或"（未找到）", '
    '"error_type": null|"concept"|"computation"|"method"|"omission", '
    '"follow_through": 布尔值（该步是否正确地沿用了自己上一步的错误结果）}],\n'
    ' "final_answer_correct": 布尔值（最终答案是否正确），\n'
    ' "summary": 一句话总评（面向学生，先肯定再指出改进点）。}\n\n'
    "判定口径：\n"
    "- hit=完整做到该步骤；partial=方向对但不完整/有瑕疵；miss=未做到或做错。\n"
    "- 学生**沿用自己的错误中间结果但推导自洽**时，该步仍判 hit 并置 "
    "follow_through=true（连锁给分：不因一处算错抹掉后续正确推导）。\n"
    "- error_type 仅在 hit 不为 hit 时填写，否则为 null。\n"
    "- 学生写了无关内容/抄题干 = 全部 miss，error_type 用 concept。\n"
    "语气参照课堂助教，温和具体，不说\"你错了\"而说\"这一步……\"。"
)


@dataclass
class RubricStep:
    """一个采分点。

    `awarded` 是本模块算出的**实际得分**，不是模型给的——见模块文档第 1 条。
    """

    no: int
    name: str
    points: float          # 归一化后的分值（各点之和 == full_score）
    hit: str               # hit / partial / miss
    awarded: float         # 实际得分 = points × 命中比例
    evidence: str = ""
    error_type: str | None = None
    follow_through: bool = False

    def to_dict(self) -> dict:
        return {
            "no": self.no,
            "name": self.name,
            "points": round(self.points, 2),
            "hit": self.hit,
            "awarded": round(self.awarded, 2),
            "evidence": self.evidence,
            "error_type": self.error_type,
            "follow_through": self.follow_through,
        }


@dataclass
class RubricResult:
    full_score: float = DEFAULT_FULL_SCORE
    score: float = 0.0
    steps: list[RubricStep] = field(default_factory=list)
    final_answer_correct: bool | None = None
    first_missed: int | None = None      # 首个丢分的步骤序号（用于定位卷面位置）
    error_type: str | None = None        # **根因**步骤的错误类型（见 root_cause_step）
    root_cause_step: int | None = None   # 根因步骤序号：首个"非下游连带"的丢分步
    affected_steps: list[int] = field(default_factory=list)  # 被根因连带的步骤
    summary: str = ""
    degraded: bool = False               # True = 批改器不可用，需回落到整体判分
    degraded_reason: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def ratio(self) -> float:
        """得分率 0~1；满分异常时为 0（而不是除零或 Infinity）。"""
        return (self.score / self.full_score) if self.full_score > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "full_score": round(self.full_score, 2),
            "score": round(self.score, 2),
            "ratio": round(self.ratio, 3),
            "steps": [s.to_dict() for s in self.steps],
            "final_answer_correct": self.final_answer_correct,
            "first_missed": self.first_missed,
            "root_cause_step": self.root_cause_step,
            "affected_steps": self.affected_steps,
            "error_type": self.error_type,
            "error_label": _ERROR_LABEL.get(self.error_type or "", ""),
            "summary": self.summary,
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
            "warnings": self.warnings,
        }


def _canonical_text(chunks: list[Chunk]) -> str:
    return "\n---\n".join(
        f"[{c.exam_point}·{c.start}-{c.end}] {c.text}" for c in chunks
    )[:3000]


def _coerce_points(raw: object) -> float:
    """把模型给的分值收敛为非负数。非法值一律 1 分（等权兜底，不是 0——
    给 0 会让该采分点在归一化后彻底消失，等于模型少拆一点就少算一分）。"""
    try:
        val = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 1.0
    if val != val or val <= 0:  # NaN 或 <= 0
        return 1.0
    return min(val, 100.0)


def _normalize(steps: list[RubricStep], full_score: float) -> None:
    """把各采分点分值**等比例缩放**到满分。

    为什么必须归一化：模型拆出的采分点分值之和常常不等于满分（拆细了 12 分、
    拆粗了 8 分）。直接累加会给出"11/10 分"这种荒谬结果，而归一化后
    "每个采分点相对重要性"这一**唯一真正来自模型的信息**被完整保留。
    """
    total = sum(s.points for s in steps)
    if total <= 0:
        # 极端兜底：等权平分，保证总和仍等于满分
        each = full_score / len(steps)
        for s in steps:
            s.points = each
        return
    scale = full_score / total
    for s in steps:
        s.points = s.points * scale


def _award(hit: str, points: float) -> float:
    if hit == HIT:
        return points
    if hit == PARTIAL:
        return points * PARTIAL_RATIO
    return 0.0


def parse_rubric(data: dict, *, full_score: float | None = None) -> RubricResult:
    """把模型的 JSON 解析为 `RubricResult`，**所有算术与校验在这里完成**。

    单独拆成纯函数是为了可测：不需要打桩 LLM 就能覆盖"权重不等于满分"
    "hit 值非法""缺 steps"等全部异常分支——这些恰恰是最容易出错的地方。
    """
    warnings: list[str] = []
    raw_full = data.get("full_score")
    try:
        parsed_full = float(raw_full)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        parsed_full = 0.0
    # 满分以调用方为准（卷面/配置），模型给的只在其合理时作为兜底
    if parsed_full <= 0 or parsed_full > 100:
        if parsed_full > 100:
            warnings.append(f"模型给出的满分 {parsed_full} 越界，已忽略")
        parsed_full = float(full_score or DEFAULT_FULL_SCORE)
    else:
        parsed_full = float(full_score or parsed_full)

    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        return RubricResult(
            degraded=True,
            degraded_reason="模型未给出采分点清单",
            warnings=warnings,
        )

    steps: list[RubricStep] = []
    for i, item in enumerate(raw_steps[:12], start=1):  # 采分点上限，防费用失控
        if not isinstance(item, dict):
            continue
        hit = str(item.get("hit") or "").strip().lower()
        if hit not in _VALID_HITS:
            # 非法值取中间档：既不冤枉学生（判 miss 会直接扣光该点），
            # 也不白送分（判 hit 会掩盖批改器异常），并留痕待复核。
            warnings.append(f"第 {i} 个采分点的 hit 值非法，按 partial 处理")
            hit = PARTIAL
        err = str(item.get("error_type") or "").strip().lower() or None
        if err is not None and err not in _VALID_ERRORS:
            warnings.append(f"第 {i} 个采分点的 error_type 非法，已置空待复核")
            err = None
        if err is not None and hit == HIT:
            err = None  # 命中就不该有错因，以命中为准
        steps.append(RubricStep(
            no=int(item.get("no") or i),
            name=str(item.get("name") or f"步骤{i}")[:30],
            points=_coerce_points(item.get("points")),
            hit=hit,
            awarded=0.0,  # 归一化后再算
            evidence=str(item.get("evidence") or "")[:200],
            error_type=err,
            follow_through=bool(item.get("follow_through")),
        ))

    if not steps:
        return RubricResult(
            degraded=True, degraded_reason="采分点清单为空", warnings=warnings,
        )

    _normalize(steps, parsed_full)
    score = 0.0
    first_missed: int | None = None
    root_cause: int | None = None
    error_type: str | None = None
    for s in steps:
        s.awarded = _award(s.hit, s.points)
        score += s.awarded
        if s.hit != HIT and first_missed is None:
            first_missed = s.no
        # 根因要**跳过 follow_through 步骤**：它是"沿用自己的错误中间结果"，
        # 性质上是下游后果。把它当错因，学生会去改一个本身没错的地方。
        if s.hit != HIT and root_cause is None and not s.follow_through:
            root_cause = s.no
            error_type = s.error_type
    # 极端兜底：所有丢分步都被标成了 follow_through（模型标注有误）时，
    # 仍需给出一个可定位的根因，否则归因会是空的。
    if root_cause is None and first_missed is not None:
        root_cause = first_missed
        error_type = next((s.error_type for s in steps if s.no == first_missed), None)
    affected = [
        s.no for s in steps
        if s.follow_through and s.hit != HIT and s.no != root_cause
    ]

    correct = data.get("final_answer_correct")
    return RubricResult(
        full_score=parsed_full,
        score=round(min(score, parsed_full), 2),   # 夹紧：浮点累加不得超满分
        steps=steps,
        final_answer_correct=bool(correct) if isinstance(correct, bool) else None,
        first_missed=first_missed,
        root_cause_step=root_cause,
        error_type=error_type,
        affected_steps=affected,
        summary=str(data.get("summary") or "")[:300],
        degraded=False,
        warnings=warnings,
    )


def _degraded(reason: str) -> RubricResult:
    return RubricResult(degraded=True, degraded_reason=reason)


async def rubric_grade(
    student_answer: str | None,
    question_text: str,
    canonical_chunks: list[Chunk],
    full_score: float | None = None,
) -> RubricResult:
    """按采分点分步批改。**判不出来时返回 degraded，绝不返回"判错"。**"""
    if not (student_answer or "").strip():
        return _degraded("学生未写作答过程")
    if not canonical_chunks:
        return _degraded("该考点暂无老师定版解法，无法拆解采分点")

    prompt = (
        f"【题目】\n{question_text}\n\n"
        f"【老师标准解法（课堂定版）】\n{_canonical_text(canonical_chunks)}\n\n"
        f"【学生作答过程】\n{student_answer[:2500]}\n\n"
        f"本题满分：{full_score if full_score else DEFAULT_FULL_SCORE:g} 分。\n"
        "请先拆解采分点，再逐点比对给分，输出 JSON。"
    )
    data = await ask_llm_json(_SYSTEM, prompt, max_tokens=1400)
    if not data:
        return _degraded("AI 批改引擎暂不可用")

    result = parse_rubric(data, full_score=full_score)
    if result.degraded:
        _logger.info("Rubric 批改降级：%s", result.degraded_reason)
    return result


def rubric_attribution(result: RubricResult) -> str:
    """面向学生的归因：定位到**根因步骤**，并点明哪些步是被它连带的。

    与 `process_diff` 的 `deviation_desc` 不同，这里一定带着采分点序号与名目，
    学生能直接回到自己卷面的对应位置。

    为什么要区分根因与连带：如果把 follow_through 的步当成错因，学生会去改
    一个本身没错的地方——真正的根因在前一步，改错了地方会越改越乱。
    但连带失分也不能**隐去**：学生需要知道"这几步的分是同一处错误导致的，
    修好根因它们会一起恢复"，否则会以为自己到处都是问题。
    """
    if result.degraded or not result.steps:
        return ""
    if result.root_cause_step is None:
        return result.summary or "各步骤均已完成"
    step = next((s for s in result.steps if s.no == result.root_cause_step), None)
    no = result.root_cause_step
    name = step.name if step else f"第 {no} 步"
    label = _ERROR_LABEL.get(result.error_type or "", "未完成")
    text = f"第 {no} 步「{name}」未得分（{label}）"
    if result.affected_steps:
        chained = "、".join(str(n) for n in result.affected_steps)
        text += f"；第 {chained} 步是沿用它推导的，修好根因后可连带恢复"
    return text


def rubric_followup(result: RubricResult) -> str | None:
    """苏格拉底式反问：针对**根因步骤**问思考依据，不直接给答案。"""
    if result.degraded or result.root_cause_step is None:
        return None
    step = next((s for s in result.steps if s.no == result.root_cause_step), None)
    if not step:
        return None
    return (
        f"第 {step.no} 步「{step.name}」这一步，你是依据什么得出这个结果的？"
        "能说说这一步用到的定理或公式吗？"
    )
