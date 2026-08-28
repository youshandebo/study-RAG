# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""轻量 Token 估算器：无外部依赖，按中英文字符加权近似。

真实模型可用时也可换成tiktoken等精确实现；当前统计口径为「估算值」，
供 Cherry Studio 式用量展示与上下文容量条使用。
"""
from __future__ import annotations

# 经验权重：CJK 文本每字符约占 0.62 token，ASCII 约 0.27 token/字符
_CJK_WEIGHT = 0.62
_ASCII_WEIGHT = 0.27


def estimate_tokens(text: str) -> int:
    """估算一段文本的 token 数（下限 1，空文本为 0）。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if ord(ch) > 0x2E80)
    other = len(text) - cjk
    return max(1, int(cjk * _CJK_WEIGHT + other * _ASCII_WEIGHT) + 1)


def breakdown_tokens(*parts: str) -> list[int]:
    return [estimate_tokens(p) for p in parts]
