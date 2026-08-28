# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""学生作答思路诊断与纠偏归因。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class GradeResult:
    correct: bool
    chosen: str
    attribution: str
    suggestion: str


_CORRECT_INDEX = 0

_ATTRIBUTIONS = {
    0: ("回答正确！", "你已经规避了「乱丢项」的审敛误区——放缩链条 $\\frac{\\ln x}{x^{3/2}} < \\frac{1}{x^{5/4}}$ 用得干净利落。"),
    1: ("选错了。", "你大概认为「含 ln 必发散」——但恰恰相反，对数的增长慢于任意正幂 $x^{\\varepsilon}$，放缩后 p=5/4>1，应当收敛。"),
    2: ("选错了。", "敛散性是与 $\\ln x$ 无关的确定结论：只要比阶得当就能判定。回忆课堂口诀「见到对数不要慌」。"),
}


class Evaluator:
    def grade_option(self, option_index: int) -> GradeResult:
        verdict, why = _ATTRIBUTIONS.get(option_index, _ATTRIBUTIONS[2])
        correct = option_index == _CORRECT_INDEX
        return GradeResult(
            correct=correct,
            chosen=chr(ord("A") + option_index) if 0 <= option_index < 26 else "?",
            attribution=f"{verdict}{why}",
            suggestion=("继续保持比阶意识！" if correct else "建议重听 24:15 的审敛误区讲解，再做一道同型变式巩固。"),
        )
