# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""意图动态路由器：识别用户输入属于 解题 / 启发伴学 / 靶向自测 / 模型比对 / 通用对话。"""
from __future__ import annotations

from app.models.domain import Intent

_SOCRATIC_KEYWORDS = ("教我", "启发", "引导", "没懂", "不懂", "不会", "为什么", "讲讲", "思路", "socratic", "explain")
_QUIZ_KEYWORDS = ("考我", "出题", "测试", "自测", "练习", "quiz", "test me", "变式")
_COMPARE_KEYWORDS = ("对比", "分屏", "比一比", "其他模型", "compare", "不同模型")
_SOLVE_HINTS = ("积分", "求导", "极限", "方程", "证明", "计算", "求解", "判断", "收敛", "发散", "矩阵", "概率")


def classify(text: str, has_image: bool, force: str | None = None) -> Intent:
    if force:
        try:
            return Intent(force)
        except ValueError:
            pass
    t = text or ""
    lowered = t.lower()
    if any(k in t or k in lowered for k in _COMPARE_KEYWORDS):
        return Intent.compare
    if any(k in t or k in lowered for k in _QUIZ_KEYWORDS):
        return Intent.quiz
    if any(k in t or k in lowered for k in _SOCRATIC_KEYWORDS):
        return Intent.socratic
    if has_image:
        return Intent.solve
    if any(k in t or k in lowered for k in _SOLVE_HINTS):
        return Intent.solve
    return Intent.general
