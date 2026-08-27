"""难度打标 (1~5 星)：综合公式复杂度、推理链长度与课堂强调强度的启发式评分。"""
from __future__ import annotations

import re

_HARD_MARKERS = ("放缩", "构造", "反证", "一致收敛", "含参", "逐项")
_EMPHASIS_MARKERS = ("注意", "易错", "误区", "陷阱", "切勿", "千万别")


def score(text: str) -> int:
    if not text:
        return 2
    level = 2
    formulas = len(re.findall(r"\$[^$]+\$|\$\$[^$]+\$\$", text))
    level += min(2, formulas // 4)
    level += sum(1 for m in _HARD_MARKERS if m in text)
    level += sum(1 for m in _EMPHASIS_MARKERS if m in text)
    return max(1, min(5, level))


def stars(level: int) -> str:
    return "⭐" * max(1, min(5, level))
