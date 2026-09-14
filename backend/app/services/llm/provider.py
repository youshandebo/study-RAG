# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""多模型 Provider 抽象层。

- OpenAICompatibleProvider: 覆盖 OpenAI / DeepSeek / Qwen(DashScope 兼容模式)
- AnthropicProvider: Claude 系列
- MockProvider: 无任何 API Key 时的本地流式演示引擎
工厂函数 get_provider() 保证调用方永远拿到可用实例。
"""
from __future__ import annotations

import abc
import json
import os
from typing import AsyncIterator

import httpx

from app.core.config import Settings, get_settings
from app.core.security import assert_safe_url


def request_timeout_s() -> float:
    """单次 LLM 请求超时。

    默认 120s 是为了容忍长回答的首包延迟；有了断路器之后，配更短的超时
    （如 20s）反而更好——故障被更快地判定为"失败"，断路器更早跳闸，
    用户不必陪着等满一次超时。
    """
    try:
        return max(1.0, float(os.getenv("LLM_REQUEST_TIMEOUT_S", "120") or 120))
    except ValueError:
        return 120.0


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
        assert_safe_url(base_url)  # SSRF 防护：拒绝内网 / 元数据端点
        self.name = name
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model

    async def stream_chat(self, messages: list[dict], system: str = "") -> AsyncIterator[str]:
        payload_messages = ([{"role": "system", "content": system}] if system else []) + messages
        payload = {"model": self._model, "messages": payload_messages, "stream": True}
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with httpx.AsyncClient(timeout=request_timeout_s()) as client:
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
        assert_safe_url(base_url)
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
        async with httpx.AsyncClient(timeout=request_timeout_s()) as client:
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

    # 管理员面板配置的主模型优先（base_url/model 支持热更新）
    from app.core import runtime_config

    main = runtime_config.effective("llm")
    if main["api_key"] and main["base_url"] and main["model"]:
        if main.get("provider") == "anthropic":
            providers["main"] = AnthropicProvider(main["api_key"], main["base_url"], main["model"])
        else:
            providers["main"] = OpenAICompatibleProvider(
                "openai-main", main["api_key"], main["base_url"], main["model"]
            )

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


def invalidate_provider_cache() -> None:
    """管理员修改模型配置后调用，强制下次请求重建 Provider。"""
    _PROVIDER_CACHE.clear()


def available_model_keys(settings: Settings | None = None) -> list[str]:
    settings = settings or get_settings()
    return list(_build_from_settings(settings).keys())


def _resilient(label: str, primary: BaseLLMProvider, skip_keys: set[str] | None = None) -> BaseLLMProvider:
    """给主通道套一层候选降级 + 每候选独立熔断。

    候选来自**其他已配置的真实供应商**（gpt → deepseek → qwen → claude）。
    同一端点同一凭证的通道不参与——它们是同一个故障域，降级过去只会一起挂。
    只有一家供应商时不包这一层（退无可退，包了纯属徒增调用栈）。

    刻意**不把 Mock 演示引擎纳入候选**：它会编造答案，比报错危险得多。
    """
    if isinstance(primary, MockProvider):
        return primary
    from app.core.circuit import ResilientLLMProvider, build, fallback_enabled

    if not fallback_enabled():
        return primary
    settings = get_settings()
    real = _build_from_settings(settings)
    primary_url = str(getattr(primary, "_base_url", "") or "")
    primary_key = str(getattr(primary, "_api_key", "") or "")
    candidates = [build(label, primary)]
    for key in ("gpt", "deepseek", "qwen", "claude"):
        if (skip_keys and key in skip_keys) or key not in real:
            continue
        other = real[key]
        if str(getattr(other, "_base_url", "") or "") == primary_url and str(
            getattr(other, "_api_key", "") or ""
        ) == primary_key:
            continue
        candidates.append(build(f"fallback:{key}", other))
    if len(candidates) == 1:
        return primary
    return ResilientLLMProvider(candidates)


def get_tier_provider(model_name: str | None) -> BaseLLMProvider:
    """会员档位模型：主通道凭证 + 档位指定模型名；无主通道回落演示引擎。"""
    from app.core import runtime_config

    main = runtime_config.effective("llm")
    if not (main["api_key"] and main["base_url"]):
        return MockProvider()
    model = (model_name or "").strip() or main["model"]
    if main.get("provider") == "anthropic":
        primary = AnthropicProvider(main["api_key"], main["base_url"], model)
    else:
        primary = OpenAICompatibleProvider(f"tier-{model}", main["api_key"], main["base_url"], model)
    return _resilient(f"main:{model}", primary)


def get_provider(model_key: str | None = None) -> BaseLLMProvider:
    """按 key 取真实 Provider；未指定时优先主模型(面板配置>环境变量)，缺失回落 Mock 演示引擎。"""
    settings = get_settings()
    real = _build_from_settings(settings)
    for k in list(_PROVIDER_CACHE):
        if k not in real or type(_PROVIDER_CACHE[k]) is not type(real[k]):
            _PROVIDER_CACHE.clear()
            break
    if not _PROVIDER_CACHE:
        _PROVIDER_CACHE.update(real)
    if model_key and model_key in real:
        return _resilient(str(model_key), real[model_key], skip_keys={str(model_key)})
    primary = _PROVIDER_CACHE.get("main")
    if primary is None:
        return MockProvider()
    return _resilient("main", primary, skip_keys={"main"})
