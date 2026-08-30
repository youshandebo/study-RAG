# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""会员档位体系：存储配额 / 模型等级 / 限速，后台 runtime_config 可调。

设计：你（平台方）承担模型与存储成本，用户按档位付费——
- free：小存储 + 平台配置的基础模型（拉新引流）
- pro / max：更大存储 + 高级模型（runtime_config.plans 指定模型名，
  复用主通道的 base_url/api_key，改模型名即换档）
- 付款对接不在本期（档位变更走管理后台手工操作 / 兑换码）
"""
from __future__ import annotations

import os

# 默认档位（runtime_config["plans"] 可逐字段覆盖，热生效）
DEFAULT_PLANS: dict[str, dict] = {
    "free": {
        "label": "免费版",
        "storage_mb": 200,          # 素材存储配额
        "model": "",                # 空 = 跟随主模型；可填更便宜的模型名
        "chat_per_min": 10,         # 提问限速（次/分钟）
        "max_upload_mb": 50,        # 单文件上限（不超过全局硬限）
    },
    "pro": {
        "label": "专业版",
        "storage_mb": 2048,
        "model": "",                # 建议后台填主力模型（如 deepseek-chat / gpt-4o-mini）
        "chat_per_min": 30,
        "max_upload_mb": 200,
    },
    "max": {
        "label": "旗舰版",
        "storage_mb": 10240,
        "model": "",                # 建议后台填最强模型
        "chat_per_min": 60,
        "max_upload_mb": 200,
    },
    "guest": {  # 未注册体验（部署方可在 env 关闭）
        "label": "体验模式",
        "storage_mb": 20,
        "model": "",
        "chat_per_min": 5,
        "max_upload_mb": 20,
    },
}


def plans() -> dict[str, dict]:
    """合并 runtime_config["plans"] 覆盖（缺字段回落默认）。"""
    try:
        from app.core.runtime_config import effective

        override = effective("plans")
        if isinstance(override, dict):
            merged = {k: dict(v) for k, v in DEFAULT_PLANS.items()}
            for tier, patch in (override or {}).items():
                if tier in merged and isinstance(patch, dict):
                    merged[tier].update({k: v for k, v in patch.items() if v not in (None, "")})
            return merged
    except Exception:
        pass
    return {k: dict(v) for k, v in DEFAULT_PLANS.items()}


def plan_for(tier: str | None) -> dict:
    return plans().get(tier or "free") or plans()["free"]


def storage_limit_bytes(tier: str | None) -> int:
    return int(plan_for(tier)["storage_mb"]) * 1024 * 1024


async def auth_required() -> bool:
    """强制鉴权开关：注册用户数 > 0 或 env 显式开启时生效。

    全新部署（无用户）保持开放演示；正式运营注册即触发全站鉴权。
    """
    if os.getenv("MEMBERSHIP_MODE", "").strip() == "1":
        return True
    try:
        from app.db.relational import count_users

        return await count_users() > 0
    except Exception:
        return False
