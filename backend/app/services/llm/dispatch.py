# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""分轨调度器：为多模型比对提供统一的流式轨道抽象。

有真实 Key 的模型走真实 API；缺失的模型自动以对应风格的内置剧本兜底，
保证任意组合下三条轨道都有差异化的并行输出。
"""
from __future__ import annotations

from typing import AsyncIterator

from app.core.config import Settings, get_settings
from app.services.llm.mock_engine import COMPARE_TRACK_TEXTS, SOLVE_MARKDOWN, tokenize


class TrackDispatcher:
    DISPLAY = {"gpt": "GPT-4o", "claude": "Claude 3.5", "qwen": "Qwen-Max", "deepseek": "DeepSeek-V3"}
    REVERSE = {"gpt-4o": "gpt", "claude-3-5": "claude", "qwen-max": "qwen", "deepseek-chat": "deepseek"}
    SCRIPT_KEY = {"gpt": "gpt-4o", "claude": "claude-3-5", "qwen": "qwen-max", "deepseek": "gpt-4o"}

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    def track_names(self, requested: list[str] | None = None) -> list[tuple[str, str]]:
        """返回 [(internal_key, display_name)]；requested 为空时取全部可用轨（真实优先，不足补 mock）。"""
        order = ["gpt", "claude", "qwen", "deepseek"]
        keys = [k for k in (requested or order) if k in self.DISPLAY]
        return [(k, self.DISPLAY[k]) for k in keys]

    def has_real(self, key: str) -> bool:
        from app.services.llm.provider import _build_from_settings

        return key in _build_from_settings(self._settings)

    async def stream(self, key: str, question: str, history: list[dict] | None = None) -> AsyncIterator[str]:
        """单轨流式输出：真实 API（含多轮历史）或风格化剧本。"""
        if self.has_real(key):
            from app.services.llm.provider import _build_from_settings

            provider = _build_from_settings(self._settings)[key]
            messages = (history or []) + [{"role": "user", "content": question}]
            async for piece in provider.stream_chat(messages):
                yield piece
            return
        script = COMPARE_TRACK_TEXTS.get(self.SCRIPT_KEY.get(key, ""), SOLVE_MARKDOWN)
        for token in tokenize(script):
            yield token
