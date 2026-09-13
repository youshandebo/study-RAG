# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""部署档位（Deployment Profile）：一套预设策略驱动跨规格服务器自适应。

三档语义
--------
- eco         1C1G / 1C2G 乞丐机：浅召回 + 小上下文 + Qdrant 全落盘 + 入库串行，
              牺牲少量检索深度换取不被 OOM Killer 杀进程。
- standard    2C4G / 4C8G：默认均衡档（未配置时的回落值）。
- performance 8C16G+：深召回 + 大上下文 + 全内存索引 + 多路入库并发。

解析规则：预设继承 + 单项覆盖
    runtime_config["deployment"]["profile"]   选择档位（留空 = standard）
    runtime_config["deployment"]["overrides"] 允许个别参数覆盖预设（None/空忽略）

消费方：chat_stream（final_top_k / token_budget）、retriever（coarse_top_k /
rerank_mode）、vector_store（qdrant_on_disk）、ingest（max_concurrent_ingest）。
所有字段每次调用现读 runtime_config，面板切换即时生效、无需重启。
"""
from __future__ import annotations

PROFILES: dict[str, dict] = {
    "eco": {
        "coarse_top_k": 10,          # 每路召回候选池深度
        "final_top_k": 3,            # 最终注入 prompt 的切片数
        "token_budget": 1800,        # 上下文装箱预算上限（token）
        "rerank_mode": "rrf_only",   # rrf_only=跳过远程精排（零外部延迟）
        "max_concurrent_ingest": 1,  # 入库全局互斥：单任务串行，保主聊天 API 的 CPU 时间片
        "qdrant_on_disk": True,      # 向量与载荷落盘，内存压制在几十 MB
        "embed_batch_size": 4,
    },
    "standard": {
        "coarse_top_k": 20,
        "final_top_k": 5,
        "token_budget": 4000,
        "rerank_mode": "api",
        "max_concurrent_ingest": 2,
        "qdrant_on_disk": True,
        "embed_batch_size": 16,
    },
    "performance": {
        "coarse_top_k": 40,
        "final_top_k": 8,
        "token_budget": 8000,
        "rerank_mode": "api",
        "max_concurrent_ingest": 4,
        "qdrant_on_disk": False,
        "embed_batch_size": 64,
    },
}

DEFAULT_PROFILE = "standard"

# 允许 overrides 覆盖的键及其合法取值约束（防面板注入非法值拖垮检索）
_OVERRIDABLE: dict[str, tuple[type, float, float]] = {
    "coarse_top_k": (int, 4, 100),
    "final_top_k": (int, 1, 20),
    "token_budget": (int, 500, 32000),
    "max_concurrent_ingest": (int, 1, 8),
    "embed_batch_size": (int, 1, 256),
}


def profile_names() -> list[str]:
    return list(PROFILES)


def resolve(raw: dict | None) -> dict:
    """把 runtime_config 的 deployment 段解析为最终生效参数。

    raw 结构：{"profile": "eco"|"standard"|"performance"|"", "overrides": {...}}
    返回预设副本 + 合法覆盖项，附带 "profile"（生效档位名）与
    "rerank_mode"（仅 rrf_only/api 两态，非法值回落预设）。
    """
    raw = raw if isinstance(raw, dict) else {}
    name = str(raw.get("profile") or "").strip().lower()
    if name not in PROFILES:
        name = DEFAULT_PROFILE
    base = dict(PROFILES[name])

    overrides = raw.get("overrides")
    if isinstance(overrides, dict):
        for key, (cast, lo, hi) in _OVERRIDABLE.items():
            val = overrides.get(key)
            if val in (None, ""):
                continue
            try:
                base[key] = int(max(lo, min(hi, cast(val))))
            except (TypeError, ValueError):
                continue  # 非法覆盖直接忽略，保持预设值
        if isinstance(overrides.get("qdrant_on_disk"), bool):
            base["qdrant_on_disk"] = overrides["qdrant_on_disk"]

    if base.get("rerank_mode") not in ("rrf_only", "api"):
        base["rerank_mode"] = PROFILES[name]["rerank_mode"]
    base["profile"] = name
    return base


def effective() -> dict:
    """从 runtime_config 现读 deployment 段并解析（面板修改即时生效）。"""
    from app.core import runtime_config

    return resolve(runtime_config.get_runtime_config().get("deployment"))
