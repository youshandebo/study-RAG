# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""部署档位（profiles）解析与配置层接线测试。

钉住三条不变量：
1. 预设继承：档位名 → 完整参数集；非法/空档位名回落 standard。
2. 单项覆盖：合法覆盖生效且被钳制到安全区间，非法覆盖被忽略。
3. runtime_config 接线：deployment 段持久化 + masked_view 下发解析后生效值。
"""
from __future__ import annotations

from app.core import profiles, runtime_config


def test_default_profile_is_standard():
    out = profiles.resolve(None)
    assert out["profile"] == "standard"
    assert out["final_top_k"] == profiles.PROFILES["standard"]["final_top_k"]


def test_eco_profile_values():
    out = profiles.resolve({"profile": "eco"})
    assert out["profile"] == "eco"
    assert out["final_top_k"] == 3
    assert out["rerank_mode"] == "rrf_only"
    assert out["max_concurrent_ingest"] == 1
    assert out["qdrant_on_disk"] is True


def test_unknown_profile_falls_back_to_standard():
    out = profiles.resolve({"profile": "turbo"})
    assert out["profile"] == "standard"


def test_override_applied_and_clamped():
    out = profiles.resolve({"profile": "eco", "overrides": {"final_top_k": 6, "coarse_top_k": 99999}})
    assert out["final_top_k"] == 6            # 合法覆盖生效
    assert out["coarse_top_k"] == 100         # 超界钳制到上限
    assert out["token_budget"] == 1800        # 未覆盖字段回落预设


def test_invalid_override_ignored():
    out = profiles.resolve({"profile": "eco", "overrides": {"final_top_k": "abc", "token_budget": None}})
    assert out["final_top_k"] == 3
    assert out["token_budget"] == 1800


def test_bool_override_qdrant_on_disk():
    out = profiles.resolve({"profile": "eco", "overrides": {"qdrant_on_disk": False}})
    assert out["qdrant_on_disk"] is False


def test_runtime_config_roundtrip():
    saved = runtime_config.save_runtime_config(
        {"deployment": {"profile": "eco", "overrides": {"final_top_k": 4}}}
    )
    assert saved["deployment"]["profile"] == "eco"
    view = runtime_config.masked_view()
    assert view["deployment"]["profile"] == "eco"
    assert view["deployment"]["final_top_k"] == 4
    assert view["deployment"]["rerank_mode"] == "rrf_only"


def test_runtime_config_invalid_profile_rejected():
    runtime_config.save_runtime_config({"deployment": {"profile": "mega"}})
    assert runtime_config.get_runtime_config()["deployment"]["profile"] == ""


def test_override_empty_value_reverts_to_preset():
    runtime_config.save_runtime_config(
        {"deployment": {"profile": "eco", "overrides": {"final_top_k": 7}}}
    )
    saved = runtime_config.save_runtime_config({"deployment": {"overrides": {"final_top_k": ""}}})
    assert "final_top_k" not in saved["deployment"]["overrides"]
    assert profiles.effective()["final_top_k"] == 3
