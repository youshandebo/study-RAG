# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""内置演示课堂语料：「10月15日 · 高等数学 · 反常积分」一节课的多模态切片。

真实场景由 ingest 流水线从录音/板书自动生成；此处为开箱即用的种子数据，
结构与流水线产物完全一致（见 chunker.Chunk）。
"""
from __future__ import annotations

AUDIO_ID = "lec-1015-hd"

from datetime import date, timedelta


def _seed_date(days_ago: int) -> str:
    """种子课程演示授课日期：默认 10 天前（随堂模式时间衰减可见）。"""
    return (date.today() - timedelta(days=days_ago)).isoformat()


SEED_CHUNKS: list[dict] = [
    {
        "id": "c-001",
        "audio_id": AUDIO_ID,
        "start": "12:05",
        "end": "14:30",
        "text": (
            r"同学们看这道题，判断 $\int_1^{+\infty} \frac{\sqrt{x}}{x^2+\ln x} dx$ 的收敛性。"
            "我们课上说过“抓大头”：$x\\to+\\infty$ 时分母里 $x^2$ 是大头，$\\ln x$ 可以扔掉，"
            "于是被积函数就相当于 $1/x^{3/2}$。"
        ),
        "board_index": 4,
        "board_caption": "板书第 04 张 - 抓大头推导原题",
        "exam_point": "p-反常积分比较审敛法",
        "difficulty": 4,
        "subject": "数学", "course_id": "math-calculus-101", "chapter": "5.3 反常积分", "lecture_date": _seed_date(10),
    },
    {
        "id": "c-002",
        "audio_id": AUDIO_ID,
        "start": "24:15",
        "end": "26:40",
        "text": (
            "注意！审敛最大的误区：抓大头不等于乱丢项。等价无穷小代换必须保证整体等价成立，"
            "不能分子丢一项、分母又丢一项。另外下限 x=1 处 ln1=0 但函数值有限，不是瑕点，别写成瑕积分。"
        ),
        "board_index": 7,
        "board_caption": "板书第 07 张 - 三大审敛误区警示",
        "exam_point": "比较审敛法常见误区",
        "difficulty": 3,
        "subject": "数学", "course_id": "math-calculus-101", "chapter": "5.3 反常积分", "lecture_date": _seed_date(10),
    },
    {
        "id": "c-003",
        "audio_id": AUDIO_ID,
        "start": "31:02",
        "end": "33:18",
        "text": (
            "p-积分判别法口诀：p 大于一收敛，p 小等于一发散。"
            r"前提是 $\int_1^{+\infty} x^{-p} dx$ 这种标准形，套之前先把函数化成标准形再比阶。"
        ),
        "board_index": 9,
        "board_caption": "板书第 09 张 - p-积分口诀与标准形",
        "exam_point": "p-积分敛散性判定定理",
        "difficulty": 2,
        "is_canonical": True, "method_version": 1,
        "subject": "数学", "course_id": "math-calculus-101", "chapter": "5.3 反常积分", "lecture_date": _seed_date(10),
    },
    {
        "id": "c-004",
        "audio_id": AUDIO_ID,
        "start": "41:50",
        "end": "44:10",
        "text": (
            r"变式训练：$\int_2^{+\infty} \frac{\ln x}{x\sqrt{x}} dx$ 收敛吗？"
            r"记住对数增长慢于任意正幂 $x^\varepsilon$，取 $\varepsilon=1/4$ 一放缩就出来了。"
        ),
        "board_index": 12,
        "board_caption": "板书第 12 张 - 对数比阶变式训练",
        "exam_point": "对数项比阶放缩技巧",
        "difficulty": 4,
        "subject": "数学", "course_id": "math-calculus-101", "chapter": "5.3 反常积分", "lecture_date": _seed_date(10),
    },
]
