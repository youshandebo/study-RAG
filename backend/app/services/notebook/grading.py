# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""复习判分：**确定性规则优先**，开放性短问答再退到结构化判分。

判分顺序与理由
--------------
1. **选项等价**（`A` ↔ 选项文本）：客观题最常见的"同一答案两种写法"，纯字符串
   比较会误判，必须先把字母映射成选项文本再比。
2. **规范化等值**：去掉 `$`、`\\left`、空格、全半角差异后逐字比较。
   学生把 `$x^2$` 写成 `x2`、把 `3/2` 写成全角，都不该判错。
3. **数值容差**：`1.5` 与 `3/2`、`1.50` 视为同一答案（相对容差 1e-6）。
4. 以上都不确定时**返回 `None`（未知）而不是 `False`**——把"没把握"和"答错"
   混为一谈，会错误地把一条其实答对的题打回 Box 1，这比判不出来更糟。
   开放式短问答交给 `structured_grade`（LLM 结构化判分），失败同样返回 `None`。
"""
from __future__ import annotations

import re

# 全角 → 半角（数字 / 字母 / 常用符号）。中文标点会干扰逐字比较，一并映射。
_FULLWIDTH = str.maketrans(
    "０１２３４５６７８９"
    "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"
    "ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ"
    "．，（）【】〔〕＝＋－×÷",
    "0123456789"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    ".,()[]()=+-x/",
)

# LaTeX / 排版噪声：这些符号的有无不影响数学含义
_NOISE_TOKENS = (
    "$", "\\(", "\\)", "\\[", "\\]", "{", "}", "\\left", "\\right",
    "\\,", "\\!", "\\;", "\\quad", "\\qquad", "\\displaystyle", "~", "`",
)

_WS_RE = re.compile(r"\s+")
_NUM_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
_FRAC_RE = re.compile(r"^([+-]?\d+)\s*/\s*(\d+)$")


def normalize(text: str) -> str:
    """把答案文本规范化到可比较形式：去 LaTeX 噪声 / 空白 / 全半角差异，转小写。"""
    out = (text or "").strip()
    out = out.translate(_FULLWIDTH)
    for token in _NOISE_TOKENS:
        out = out.replace(token, "")
    out = out.replace("\\cdot", "*").replace("\\times", "*")
    out = out.replace("\\div", "/")
    out = _WS_RE.sub("", out)
    out = out.rstrip(".。")
    return out.lower()


def as_number(text: str) -> float | None:
    """能解析成数值则返回，否则 None（支持 `3/2` 这样的简单分数）。"""
    t = (text or "").strip()
    if _NUM_RE.match(t):
        try:
            return float(t)
        except ValueError:
            return None
    frac = _FRAC_RE.match(t)
    if frac:
        denom = int(frac.group(2))
        if denom == 0:
            return None
        return int(frac.group(1)) / denom
    return None


def _numeric_equal(a: float, b: float) -> bool:
    return abs(a - b) <= max(1e-9, 1e-6 * abs(b))


def grade_answer(
    *,
    reference_answer: str | None,
    student_answer: str | None,
    options: list[str] | None = None,
) -> tuple[bool | None, str]:
    """确定性判分。返回 `(True|False|None, 说明)`；`None` = 无法确定性判定。"""
    stu = (student_answer or "").strip()
    if not stu:
        return None, "未作答"

    ref = (reference_answer or "").strip()
    opts = [str(o) for o in (options or [])]

    # 1) 选项字母 ↔ 选项文本等价
    if opts and ref:
        alias = {chr(ord("A") + i): opt for i, opt in enumerate(opts)}
        stu_mapped = alias.get(stu.upper(), stu)
        ref_mapped = alias.get(ref.upper(), ref)
        if normalize(stu_mapped) == normalize(ref_mapped) or stu.upper() == ref.upper():
            return True, "选项匹配"
        # 学生确实选了某一个合法选项，但选错 → 明确判错
        if stu.upper() in alias:
            return False, f"正确答案：{ref}"

    if not ref:
        return None, "本题无标准答案"

    # 2) 规范化逐字等值
    if normalize(stu) and normalize(stu) == normalize(ref):
        return True, "答案等价"

    # 3) 数值容差
    fs, fr = as_number(normalize(stu)), as_number(normalize(ref))
    if fs is not None and fr is not None:
        if _numeric_equal(fs, fr):
            return True, "数值等价"
        return False, f"正确答案：{ref}"

    # 4) 有选项却对不上任何选项 → 判错；否则不确定，交给结构化判分
    if opts:
        return False, f"正确答案：{ref}"
    return None, "无法确定性判分"


_GRADE_SYSTEM = (
    "你是严谨的阅卷助教。只输出一个 JSON 对象，不要任何多余文字。"
    '字段：correct（布尔，学生答案在数学上是否等价于参考答案）、'
    'reason（一句话说明，≤60 字）。判定只看数学等价性，不要因表述差异判错。'
)


async def structured_grade(
    *,
    question: str = "",
    reference_answer: str = "",
    student_answer: str = "",
) -> tuple[bool | None, str]:
    """开放性短问答的结构化判分：LLM 返回严格 JSON。

    任何异常 / 非 JSON / 缺字段一律返回 `(None, ...)`——判分器不可用时
    **不能倒向"判错"**，否则会把技术故障变成学生的掌握度惩罚。
    """
    try:
        from app.services.llm.json_call import ask_llm_json

        prompt = (
            f"题目：{question[:600]}\n"
            f"参考答案：{reference_answer[:300]}\n"
            f"学生作答：{student_answer[:300]}\n"
            "请判断学生作答是否与参考答案数学等价。输出 JSON。"
        )
        data = await ask_llm_json(_GRADE_SYSTEM, prompt)
        if not isinstance(data, dict) or "correct" not in data:
            return None, "结构化判分不可用"
        return bool(data["correct"]), str(data.get("reason") or "")[:120]
    except Exception:  # noqa: BLE001 - 判分不可用不得升级为判错
        return None, "结构化判分不可用"


async def grade_with_fallback(
    *,
    reference_answer: str | None,
    student_answer: str | None,
    options: list[str] | None = None,
    question: str = "",
) -> tuple[bool | None, str]:
    """先走确定性规则；不确定时才退到结构化判分。"""
    verdict, reason = grade_answer(
        reference_answer=reference_answer, student_answer=student_answer, options=options,
    )
    if verdict is not None:
        return verdict, reason
    return await structured_grade(
        question=question, reference_answer=reference_answer or "", student_answer=student_answer or "",
    )
