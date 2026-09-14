# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""Leitner 盒子复习调度（纯函数，零 IO）。

为什么是 Leitner，而不是"自适应遗忘曲线 / SM-2 / 各种带权重的黑盒"
----------------------------------------------------------------
错题复习的本质诉求很简单：**答对了就把它推远一点再考，答错了就拉回来重考**。
Leitner 盒子用五个离散档位精确表达了这件事，且每个决策都能被人工复算——
"为什么这条 14 天后才复习"永远能回答清楚。

相比之下，SM-2 之类算法引入 ease factor、质量评分等若干可调参数，一旦
学生反馈"怎么老考我同一题 / 怎么再也不考了"，排查者无从判断是数据问题
还是参数问题。本项目明确选择**可验证的简单规则**而非"看起来更智能"的模型。

档位与间隔
----------
Box 1 → 1 天, Box 2 → 3 天, Box 3 → 7 天, Box 4 → 14 天, Box 5 → 30 天。
达到 Box 5 即视为已攻克（`mastered`），不再进入待复习队列。
"""
from __future__ import annotations

DAY_MS = 86_400_000

MIN_BOX = 1
MAX_BOX = 5
MASTERED_BOX = 5

# 每个盒子对应的复习间隔（天）。调这里就能整体改变复习节奏。
BOX_INTERVALS_DAYS: dict[int, int] = {1: 1, 2: 3, 3: 7, 4: 14, 5: 30}

# 答对 / 答错的掌握度增减。刻意用固定步长而非指数加权——
# 固定步长意味着"连续答对 4 次即从 40 分到满"，行为可预期。
MASTERY_GAIN = 15
MASTERY_PENALTY = 25

STATUS_ACTIVE = "active"
STATUS_MASTERED = "mastered"
STATUS_ARCHIVED = "archived"


def clamp(value: int, low: int = 0, high: int = 100) -> int:
    return max(low, min(high, int(value)))


def interval_days(box: int) -> int:
    """盒子档位 → 复习间隔天数（越界钳到边界盒）。"""
    return BOX_INTERVALS_DAYS.get(max(MIN_BOX, min(MAX_BOX, int(box))), BOX_INTERVALS_DAYS[MIN_BOX])


def initial_mastery(fail_count: int = 1) -> int:
    """初始掌握度：失败次数越多起点越低（默认落在低分区）。

    起点给低分是**故意**的：一条刚被判错的题若显示掌握度 80，
    教研看板和学生的直觉都会被误导。
    """
    return clamp(40 - (max(1, int(fail_count)) - 1) * 10)


def schedule_ms(box: int, now_ms: int) -> int:
    """按盒子计算下次复习时间戳（毫秒）。"""
    return int(now_ms) + interval_days(box) * DAY_MS


def is_due(next_review_at: int, now_ms: int) -> bool:
    return int(next_review_at or 0) <= int(now_ms)


def on_correct(box: int, mastery: int, now_ms: int) -> dict:
    """复习答对：升一盒、加掌握度、按新盒推迟下次复习。

    升到 `MASTERED_BOX` 即标记 `mastered`——此时仍给出 `next_review_at`
    （30 天后），留一个"很久以后抽查一次"的余地，而不是彻底遗忘。
    """
    new_box = min(MAX_BOX, max(MIN_BOX, int(box)) + 1)
    new_mastery = clamp(int(mastery) + MASTERY_GAIN)
    return {
        "leitner_box": new_box,
        "mastery_score": new_mastery,
        "status": STATUS_MASTERED if new_box >= MASTERED_BOX else STATUS_ACTIVE,
        "next_review_at": schedule_ms(new_box, now_ms),
    }


def on_wrong(box: int, mastery: int, now_ms: int) -> dict:
    """复习答错：**直接打回 Box 1**、扣掌握度、次日重考。

    重置到 Box 1 而非降一档，是 Leitner 的核心：答错说明这条还没真正掌握，
    继续按"半记得"的节奏放远只会让它反复沉没。
    """
    return {
        "leitner_box": MIN_BOX,
        "mastery_score": clamp(int(mastery) - MASTERY_PENALTY),
        "status": STATUS_ACTIVE,
        "next_review_at": schedule_ms(MIN_BOX, now_ms),
    }


def new_entry(fail_count: int = 1, now_ms: int = 0) -> dict:
    """新入错题本的初始元数据。"""
    return {
        "leitner_box": MIN_BOX,
        "mastery_score": initial_mastery(fail_count),
        "review_count": 0,
        "last_reviewed_at": 0,
        "status": STATUS_ACTIVE,
        "next_review_at": schedule_ms(MIN_BOX, now_ms),
    }
