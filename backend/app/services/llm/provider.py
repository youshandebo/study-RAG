"""多模型 Provider 抽象层。

- OpenAICompatibleProvider: 覆盖 OpenAI / DeepSeek / Qwen(DashScope 兼容模式)
- AnthropicProvider: Claude 系列
- MockProvider: 无任何 API Key 时的本地流式演示引擎
工厂函数 get_provider() 保证调用方永远拿到可用实例。
"""
from __future__ import annotations

import abc
import json
from typing import AsyncIterator

import httpx

from app.core.config import Settings, get_settings


class BaseLLMProvider(abc.ABC):
    name: str = "base"

    @abc.abstractmethod
    async def stream_chat(self, messages: list[dict], system: str = "") -> AsyncIterator[str]:
        """以增量文本形式流式返回模型回复。"""

    async def complete(self, messages: list[dict], system: str = "") -> str:
        chunks: list[str] = []
        async for piece in self.stream_chat(messages, system):
            chunks.append(piece)
        return "".join(chunks)


class MockProvider(BaseLLMProvider):
    name = "mock-engine"

    def __init__(self, scripted: str = "", chunk_delay: float = 0.018) -> None:
        from app.services.llm import mock_engine

        self._engine = mock_engine
        self._scripted = scripted
        self._delay = chunk_delay

    async def stream_chat(self, messages: list[dict], system: str = "") -> AsyncIterator[str]:
        prompt = messages[-1]["content"] if messages else ""
        text = self._scripted or self._engine.reply_for(prompt)
        for token in self._engine.tokenize(text):
            yield token


class OpenAICompatibleProvider(BaseLLMProvider):
    """OpenAI / DeepSeek / Qwen(DashScope compatible-mode) 共用协议。"""

    def __init__(self, name: str, api_key: str, base_url: str, model: str) -> None:
        self.name = name
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model

    async def stream_chat(self, messages: list[dict], system: str = "") -> AsyncIterator[str]:
        payload_messages = ([{"role": "system", "content": system}] if system else []) + messages
        payload = {"model": self._model, "messages": payload_messages, "stream": True}
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream(
                "POST", f"{self._base_url}/chat/completions", json=payload, headers=headers
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        delta = json.loads(data)["choices"][0].get("delta", {})
                        piece = delta.get("content")
                        if piece:
                            yield piece
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue


class AnthropicProvider(BaseLLMProvider):
    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        self.name = "anthropic"
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model

    async def stream_chat(self, messages: list[dict], system: str = "") -> AsyncIterator[str]:
        payload = {
            "model": self._model,
            "max_tokens": 4096,
            "stream": True,
            "messages": messages,
        }
        if system:
            payload["system"] = system
        headers = {"x-api-key": self._api_key, "anthropic-version": "2023-06-01"}
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream(
                "POST", f"{self._base_url}/v1/messages", json=payload, headers=headers
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") == "content_block_delta":
                        piece = event.get("delta", {}).get("text")
                        if piece:
                            yield piece


def _build_from_settings(settings: Settings) -> dict[str, BaseLLMProvider]:
    providers: dict[str, BaseLLMProvider] = {}
    if settings.openai_api_key:
        providers["gpt"] = OpenAICompatibleProvider(
            "openai", settings.openai_api_key, settings.openai_base_url, settings.openai_model
        )
    if settings.deepseek_api_key:
        providers["deepseek"] = OpenAICompatibleProvider(
            "deepseek", settings.deepseek_api_key, settings.deepseek_base_url, settings.deepseek_model
        )
    if settings.dashscope_api_key:
        providers["qwen"] = OpenAICompatibleProvider(
            "qwen", settings.dashscope_api_key, settings.dashscope_base_url, settings.qwen_model
        )
    if settings.anthropic_api_key:
        providers["claude"] = AnthropicProvider(
            settings.anthropic_api_key, settings.anthropic_base_url, settings.anthropic_model
        )
    return providers


_PROVIDER_CACHE: dict[str, BaseLLMProvider] = {}


def available_model_keys(settings: Settings | None = None) -> list[str]:
    settings = settings or get_settings()
    return list(_build_from_settings(settings).keys())


def get_provider(model_key: str | None = None) -> BaseLLMProvider:
    """按 key 取真实 Provider；缺失或未配置时一律回落 Mock 演示引擎。"""
    settings = get_settings()
    real = _build_from_settings(settings)
    if not _PROVIDER_CACHE:
        _PROVIDER_CACHE.update(real)
    if model_key and model_key in real:
        return real[model_key]
    return MockProvider()
