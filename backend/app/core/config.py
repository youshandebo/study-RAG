# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""全局配置：环境变量驱动，所有外部依赖均可选，缺失时自动降级 Mock 模式。"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _has(key: str) -> bool:
    return bool(os.getenv(key, "").strip())


def _tri_bool(key: str) -> bool | None:
    """三态布尔：显式 1/0 生效，未配置返回 None（交由业务按场景推导）。"""
    raw = os.getenv(key, "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    app_name: str = "AI Classroom Tutor"
    api_prefix: str = "/api/v1"

    # ---- 多模型调度配置（留空即走 Mock 兜底）----
    openai_api_key: str = Field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    openai_base_url: str = Field(default_factory=lambda: os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    openai_model: str = Field(default_factory=lambda: os.getenv("OPENAI_MODEL", "gpt-4o"))

    anthropic_api_key: str = Field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    anthropic_base_url: str = Field(default_factory=lambda: os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com"))
    anthropic_model: str = Field(default_factory=lambda: os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest"))

    dashscope_api_key: str = Field(default_factory=lambda: os.getenv("DASHSCOPE_API_KEY", ""))
    dashscope_base_url: str = Field(
        default_factory=lambda: os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    )
    qwen_model: str = Field(default_factory=lambda: os.getenv("QWEN_MODEL", "qwen-max"))

    deepseek_api_key: str = Field(default_factory=lambda: os.getenv("DEEPSEEK_API_KEY", ""))
    deepseek_base_url: str = Field(default_factory=lambda: os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"))
    deepseek_model: str = Field(default_factory=lambda: os.getenv("DEEPSEEK_MODEL", "deepseek-chat"))

    # ---- 向量 / 关系库 / 对象存储（可选）----
    qdrant_url: str = Field(default_factory=lambda: os.getenv("QDRANT_URL", ""))
    qdrant_collection: str = "classroom_chunks"
    postgres_dsn: str = Field(default_factory=lambda: os.getenv("POSTGRES_DSN", ""))
    redis_url: str = Field(default_factory=lambda: os.getenv("REDIS_URL", ""))
    minio_endpoint: str = Field(default_factory=lambda: os.getenv("MINIO_ENDPOINT", ""))
    minio_access_key: str = Field(default_factory=lambda: os.getenv("MINIO_ACCESS_KEY", ""))
    minio_secret_key: str = Field(default_factory=lambda: os.getenv("MINIO_SECRET_KEY", ""))

    embedding_backend: Literal["openai", "hash"] = Field(
        default_factory=lambda: "openai" if _has("OPENAI_API_KEY") else "hash"
    )

    # ---- 演示内容与展示口径 ----
    # 是否把内置演示语料灌入知识库。未显式配置时：仅在演示模式（无任何真实 Key）
    # 下灌入，配了真实模型即视为真实部署，不再用演示语料污染知识库。
    seed_demo_corpus: bool | None = Field(default_factory=lambda: _tri_bool("SEED_DEMO_CORPUS"))
    # 上下文容量上限（tokens）：前端容量条按此换算，换模型时改成对应窗口大小
    context_limit: int = Field(default_factory=lambda: int(os.getenv("CONTEXT_LIMIT", "131072") or 131072))

    @property
    def mock_mode(self) -> bool:
        """无任何真实 Key 时自动进入内置演示引擎模式。"""
        return not any(
            [
                self.openai_api_key,
                self.anthropic_api_key,
                self.dashscope_api_key,
                self.deepseek_api_key,
            ]
        )

    @property
    def seed_demo_corpus_effective(self) -> bool:
        """演示语料是否入库：显式配置优先，未配置时跟随演示模式。"""
        return self.mock_mode if self.seed_demo_corpus is None else self.seed_demo_corpus


@lru_cache
def get_settings() -> Settings:
    return Settings()
