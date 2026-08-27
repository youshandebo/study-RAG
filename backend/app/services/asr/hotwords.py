"""专业学科词表热词增强纠偏：对 ASR 输出做术语级替换修复。"""
from __future__ import annotations

HOTWORDS: dict[str, str] = {
    "反常积分": "反常积分",
    "反常极分": "反常积分",
    "繁常积分": "反常积分",
    "审敛法": "审敛法",
    "甚敛法": "审敛法",
    "神链法": "审敛法",
    "抓大头": "抓大头",
    "瑕点": "瑕点",
    "侠点": "瑕点",
    "崩点": "瑕点",
    "比阶": "比阶",
    "笔接": "比阶",
    "等价无穷小": "等价无穷小",
    "等价无穹小": "等价无穷小",
    "洛必达": "洛必达",
    "落必达": "洛必达",
    "泰勒展开": "泰勒展开",
    "太勒展开": "泰勒展开",
}


def correct(text: str) -> str:
    for wrong, right in HOTWORDS.items():
        text = text.replace(wrong, right)
    return text
