# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""LLM 结构化 JSON 调用工具：出题器 / 批改器共用。失败返回 None 由调用方降级。"""
from __future__ import annotations

import json
import re


def extract_json_block(text: str) -> dict | None:
    """从模型回复里抠出第一个 JSON 对象（容忍 ```json 围栏与前后闲话）。"""
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        start = text.find("{")
        if start < 0:
            return None
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    break
    if not candidate:
        return None
    try:
        data = json.loads(candidate)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


async def ask_llm_json(system: str, user_prompt: str, max_tokens: int = 1200) -> dict | None:
    """用当前主模型（面板配置>环境变量>演示引擎）做一次 JSON 生成。

    演示引擎的输出不是 JSON 时自然解析失败返回 None，调用方走降级路径。
    """
    try:
        from app.services.llm.provider import get_provider

        provider = get_provider()
        reply = await provider.complete(
            [{"role": "user", "content": user_prompt}],
            system=system,
        )
        return extract_json_block(reply)
    except Exception:
        return None
