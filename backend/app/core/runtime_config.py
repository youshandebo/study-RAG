# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""运行时模型配置：管理员面板驱动的持久化配置层。

- 配置落盘 JSON（backend/data/runtime_config.json），修改即时生效、重启保留。
- 各字段留空时自动回落 .env 环境变量默认值，保证"环境变量首次部署 / 面板二次调整"两级配置语义。
- API Key 读取时打码返回，仅当提交值非掩码时才覆盖存储。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import pathlib
import threading
import time

from app.core.config import get_settings

_DATA_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "data"
_CONFIG_PATH = _DATA_DIR / "runtime_config.json"

_MASK_PREFIX = "******"

_lock = threading.Lock()
_cache: dict | None = None
_cache_mtime: float = -1.0


def _blank() -> dict:
    return {
        "admin_password_hash": "",
        "admin_email": "",
        "llm": {"provider": "", "base_url": "", "api_key": "", "model": ""},
        "embedding": {"base_url": "", "api_key": "", "model": ""},
        "asr": {"base_url": "", "api_key": "", "model": ""},
        "vlm": {"base_url": "", "api_key": "", "model": ""},
        # 媒体压缩参数（数值以字符串形式存储，effective() 负责转型）
        "media": {"image_quality": "", "image_max_edge": "", "audio_bitrate": "", "audio_max_mb": ""},
        # 检索调权：预设档位 + 可选的原始系数覆盖（留空=跟随预设）
        # fusion：rrf（倒数排名融合，默认）| weighted（加权和）
        "retrieval": {"profile": "", "fusion": "", "vector": "", "lexical": "", "bm25": "", "canonical_bonus": "", "time_alpha": ""},
        # 二阶段精排（远程 Rerank API）：默认关闭，走 NoopReranker 零依赖兜底
        "rerank": {
            "enabled": "",
            "protocol": "",
            "api_base": "",
            "api_key": "",
            "api_model": "",
            "timeout": "",
            "tau": "",
            "beta": "",
            "recall_pool": "",
        },
        # 会员档位：按 tier 覆盖 storage_mb/model/chat_per_min/max_upload_mb
        "plans": {},
        # 部署档位：eco/standard/performance + 可选单项覆盖（overrides 留空=跟随预设）
        "deployment": {"profile": "", "overrides": {}},
    }


_MEDIA_KEYS = ("image_quality", "image_max_edge", "audio_bitrate", "audio_max_mb")


def _normalize(raw: dict | None) -> dict:
    """补齐缺失键，丢弃未知键；只保留字符串字段。"""
    cfg = _blank()
    if not isinstance(raw, dict):
        return cfg
    cfg["admin_password_hash"] = str(raw.get("admin_password_hash") or "")
    cfg["admin_email"] = str(raw.get("admin_email") or "").strip().lower()
    for section in ("llm", "embedding", "asr", "vlm"):
        src = raw.get(section)
        if isinstance(src, dict):
            for key in list(cfg[section]):
                val = src.get(key)
                if isinstance(val, str):
                    cfg[section][key] = val.strip()
    media_src = raw.get("media")
    if isinstance(media_src, dict):
        for key in _MEDIA_KEYS:
            val = media_src.get(key)
            if val is not None and str(val).strip() != "":
                cfg["media"][key] = str(int(val))
    plans_src = raw.get("plans")
    if isinstance(plans_src, dict):
        for tier, patch in plans_src.items():
            if isinstance(patch, dict):
                cfg["plans"].setdefault(str(tier), {}).update(
                    {str(k): v for k, v in patch.items() if v not in (None, "")}
                )
    retrieval_src = raw.get("retrieval")
    if isinstance(retrieval_src, dict):
        cfg["retrieval"]["profile"] = str(retrieval_src.get("profile") or "").strip()
        cfg["retrieval"]["fusion"] = str(retrieval_src.get("fusion") or "").strip()
        for key in ("vector", "lexical", "bm25", "canonical_bonus", "time_alpha"):
            val = retrieval_src.get(key)
            if val is not None and str(val).strip() != "":
                cfg["retrieval"][key] = str(float(val))
    rerank_src = raw.get("rerank")
    if isinstance(rerank_src, dict):
        for key in (
            "enabled",
            "protocol",
            "api_base",
            "api_key",
            "api_model",
            "timeout",
            "tau",
            "beta",
            "recall_pool",
        ):
            val = rerank_src.get(key)
            if val is not None and str(val).strip() != "":
                cfg["rerank"][key] = str(val).strip()
    dep_src = raw.get("deployment")
    if isinstance(dep_src, dict):
        cfg["deployment"]["profile"] = str(dep_src.get("profile") or "").strip().lower()
        ov = dep_src.get("overrides")
        if isinstance(ov, dict):
            cfg["deployment"]["overrides"] = {
                str(k): v for k, v in ov.items() if v not in (None, "")
            }
    return cfg


def _load_from_disk() -> dict:
    try:
        if _CONFIG_PATH.exists():
            return _normalize(json.loads(_CONFIG_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass
    return _normalize(None)


def get_runtime_config() -> dict:
    """读取运行时配置（带 mtime 缓存，多进程/外部改文件也能感知）。"""
    global _cache, _cache_mtime
    with _lock:
        try:
            mtime = _CONFIG_PATH.stat().st_mtime if _CONFIG_PATH.exists() else -1.0
        except OSError:
            mtime = -1.0
        if _cache is None or mtime != _cache_mtime:
            _cache = _load_from_disk()
            _cache_mtime = mtime
        return json.loads(json.dumps(_cache, ensure_ascii=False))  # 深拷贝快照


def save_runtime_config(patch: dict) -> dict:
    """按字段合并写入：patch 中出现的键才覆盖（含空串，空=回落默认）。

    媒体分区的数值字段做 int 校验（1-100 质量、256-8192 边长、16-64 码率、1-500 体积）。
    """
    global _cache, _cache_mtime
    current = _load_from_disk()
    patch = patch if isinstance(patch, dict) else {}
    if isinstance(patch.get("plans"), dict):
        current["plans"] = _normalize({"plans": patch["plans"]}).get("plans", {})
    # 部署档位：profile 校验合法性 + overrides 原样合并（数值约束在 profiles.resolve 内完成）
    dep = patch.get("deployment")
    if isinstance(dep, dict):
        from app.core.profiles import PROFILES

        target = current.setdefault("deployment", {"profile": "", "overrides": {}})
        if "profile" in dep:
            name = str(dep.get("profile") or "").strip().lower()
            target["profile"] = name if name in PROFILES else ""
        if isinstance(dep.get("overrides"), dict):
            merged = dict(target.get("overrides") or {})
            for k, v in dep["overrides"].items():
                if v in (None, ""):
                    merged.pop(str(k), None)  # 空值 = 撤销该覆盖，回落预设
                else:
                    merged[str(k)] = v
            target["overrides"] = merged
    for section in ("llm", "embedding", "asr", "vlm", "media", "retrieval", "rerank"):
        src = patch.get(section)
        if not isinstance(src, dict):
            continue
        target = current.setdefault(section, {})
        for key in list(target):
            if key not in src:
                continue
            val = src[key]
            if section == "retrieval" and key not in ("profile", "fusion"):
                try:
                    float(val)
                except (TypeError, ValueError):
                    continue
                target[key] = str(float(val))
            elif section == "rerank" and key in ("tau", "beta", "recall_pool", "timeout"):
                try:
                    float(val)
                except (TypeError, ValueError):
                    continue  # 非法数值直接忽略，保持现值
                target[key] = str(float(val))
            elif section == "media":
                try:
                    int(val)
                except (TypeError, ValueError):
                    continue  # 非法数值直接忽略，保持现值
                target[key] = str(int(val))
            elif isinstance(val, str):
                target[key] = val.strip()
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_CONFIG_PATH)
    with _lock:
        _cache = current
        _cache_mtime = _CONFIG_PATH.stat().st_mtime
    on_config_changed()
    return current


# ------------------------------------------------------------------ hashing --
def set_admin_password(password: str) -> None:
    """网页初始化/后台改密：写入加盐哈希（同 admin.py change_password 通道）。"""
    from app.core.security import hash_password

    with _lock:
        current = _load_from_disk()
        current["admin_password_hash"] = hash_password(password)
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_CONFIG_PATH)
        globals()["_cache"] = current
        globals()["_cache_mtime"] = _CONFIG_PATH.stat().st_mtime


def set_admin_email(email: str) -> None:
    """初始化向导写入平台管理员邮箱（用于 /auth/me 判定 is_admin，控制
    工作台"管理后台"入口仅对管理员可见）。"""
    with _lock:
        current = _load_from_disk()
        current["admin_email"] = email.strip().lower()
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_CONFIG_PATH)
        globals()["_cache"] = current
        globals()["_cache_mtime"] = _CONFIG_PATH.stat().st_mtime


def verify_admin_password(password: str) -> bool:
    from app.core.security import verify_password

    stored = get_runtime_config().get("admin_password_hash", "")
    if not stored:
        stored = os.getenv("ADMIN_PASSWORD", "").strip()
    if not stored:
        return password == "admin123"  # 首次部署默认口令，登录后请修改
    if "$" not in stored:
        return password == stored  # 兼容环境变量直接写明文口令
    # 主路径：PBKDF2（security.hash_password 写入）；兼容存量 SHA256(salt+pwd) 哈希
    if verify_password(password, stored):
        return True
    salt, digest = stored.split("$", 1)
    legacy = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return hmac.compare_digest(digest, legacy)


# ------------------------------------------------------------ effective view --
def _env_default_llm() -> dict:
    s = get_settings()
    # 默认主 LLM：OpenAI > Qwen > DeepSeek > Claude > Anthropic 协议自定义
    if s.openai_api_key:
        return {"provider": "openai-compatible", "base_url": s.openai_base_url, "api_key": s.openai_api_key, "model": s.openai_model}
    if s.dashscope_api_key:
        return {"provider": "openai-compatible", "base_url": s.dashscope_base_url, "api_key": s.dashscope_api_key, "model": s.qwen_model}
    if s.deepseek_api_key:
        return {"provider": "openai-compatible", "base_url": s.deepseek_base_url, "api_key": s.deepseek_api_key, "model": s.deepseek_model}
    if s.anthropic_api_key:
        return {"provider": "anthropic", "base_url": s.anthropic_base_url, "api_key": s.anthropic_api_key, "model": s.anthropic_model}
    return {"provider": "", "base_url": "", "api_key": "", "model": ""}


def effective(kind: str):
    """解析某类配置的最终生效值：面板显式配置 > 内置默认 / .env 推导。"""
    rc = get_runtime_config()
    settings = get_settings()

    if kind == "plans":
        return rc.get("plans") or {}

    if kind == "retrieval":
        # 预设档位 + 高级覆盖：未覆盖字段回落预设值
        #
        # fusion 决定候选池内多路信号如何合成：
        #   rrf      —— 倒数排名融合（默认）。只看名次不看分值，天然免疫
        #               "两路分数量纲不可比"（长查询 vs 短查询、BM25 长尾 vs
        #               余弦密集）。夹具实测 MRR 0.90（加权和 0.80）。
        #               融合分经语义调制后仍可作绝对阈值使用。
        #   weighted —— 加权和。分数是绝对量纲，直接与 min_score 配套，
        #               但需按场景人工配平权重（易受查询长度影响）。
        presets = {
            "strict":   {"vector": 0.45, "lexical": 0.15, "bm25": 0.15, "canonical_bonus": 0.5, "time_alpha": 0.30},
            "balanced": {"vector": 0.50, "lexical": 0.20, "bm25": 0.20, "canonical_bonus": 0.3, "time_alpha": 0.20},
            "explore":  {"vector": 0.65, "lexical": 0.20, "bm25": 0.15, "canonical_bonus": 0.0, "time_alpha": 0.0},
        }
        section = rc.get("retrieval", {})
        profile = section.get("profile") if section.get("profile") in presets else "balanced"
        fusion = str(section.get("fusion") or "").strip().lower()
        if fusion not in ("weighted", "rrf"):
            # 默认 RRF：夹具实测 MRR 0.90（加权和 0.80），且免疫两路分值的尺度漂移
            fusion = "rrf"
        out = {"profile": profile, "fusion": fusion, **presets[profile]}
        for key in ("vector", "lexical", "bm25", "canonical_bonus", "time_alpha"):
            if str(section.get(key) or "").strip():
                try:
                    out[key] = max(0.0, min(1.0, float(section[key])))
                except (TypeError, ValueError):
                    pass
        return out

    if kind == "rerank":
        from app.services.rag.reranker import (
            DEFAULT_API_MODEL,
            DEFAULT_BETA,
            DEFAULT_PROTOCOL,
            DEFAULT_RECALL_POOL,
            DEFAULT_TAU,
            DEFAULT_TIMEOUT_S,
        )

        section = rc.get("rerank", {})
        enabled = str(section.get("enabled") or "").strip().lower() in ("1", "true", "yes", "on")
        protocol = str(section.get("protocol") or "").strip().lower()
        if protocol not in DEFAULT_API_MODEL:
            protocol = DEFAULT_PROTOCOL

        def _num(key: str, default: float, lo: float, hi: float) -> float:
            raw = str(section.get(key) or "").strip()
            if not raw:
                return default
            try:
                return max(lo, min(hi, float(raw)))
            except (TypeError, ValueError):
                return default

        return {
            "enabled": enabled,
            "protocol": protocol,
            "api_base": str(section.get("api_base") or "").strip(),
            "api_key": str(section.get("api_key") or "").strip(),
            "api_model": str(section.get("api_model") or "").strip(),
            "timeout": _num("timeout", DEFAULT_TIMEOUT_S, 1.0, 60.0),
            "tau": _num("tau", DEFAULT_TAU, 0.0, 1.0),
            "beta": _num("beta", DEFAULT_BETA, 0.0, 5.0),
            "recall_pool": int(_num("recall_pool", DEFAULT_RECALL_POOL, 1, 100)),
        }

    if kind == "media":
        # 媒体压缩参数：面板值 > 内置默认（compressor.DEFAULTS 再兜底）
        defaults = {"image_quality": 82, "image_max_edge": 2560, "audio_bitrate": 24, "audio_max_mb": 200}
        section = rc.get("media", {})
        out = {}
        for key, dft in defaults.items():
            try:
                out[key] = int(section.get(key) or dft)
            except (TypeError, ValueError):
                out[key] = dft
        # 业务上限约束
        out["image_quality"] = min(100, max(1, out["image_quality"]))
        out["image_max_edge"] = min(8192, max(256, out["image_max_edge"]))
        out["audio_bitrate"] = min(64, max(16, out["audio_bitrate"]))
        out["audio_max_mb"] = min(500, max(1, out["audio_max_mb"]))
        return out

    section = dict(rc.get(kind, {}))

    if kind == "llm":
        env = _env_default_llm()
        base = {**env, **{k: v for k, v in section.items() if v}}
        provider = base.get("provider") or env.get("provider") or ""
        return {"provider": provider, **{k: base.get(k, "") for k in ("base_url", "api_key", "model")}}

    if kind == "embedding":
        env = (
            {"base_url": settings.openai_base_url, "api_key": settings.openai_api_key, "model": "text-embedding-3-small"}
            if settings.openai_api_key
            else {"base_url": "", "api_key": "", "model": ""}
        )
        base = {**env, **{k: v for k, v in section.items() if v}}
        return {k: base.get(k, "") for k in ("base_url", "api_key", "model")}

    if kind == "vlm":
        env = (
            {"base_url": settings.dashscope_base_url, "api_key": settings.dashscope_api_key, "model": "qwen-vl-max"}
            if settings.dashscope_api_key
            else {"base_url": "", "api_key": "", "model": ""}
        )
        base = {**env, **{k: v for k, v in section.items() if v}}
        return {k: base.get(k, "") for k in ("base_url", "api_key", "model")}

    # asr：无环境默认，配置了才启用远程转录
    return {k: section.get(k, "") for k in ("base_url", "api_key", "model")}


def is_masked(value: str) -> bool:
    return bool(value) and value.startswith(_MASK_PREFIX)


def mask_key(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 6:
        return f"{_MASK_PREFIX}"
    return f"{_MASK_PREFIX}{value[-4:]}"


def masked_view() -> dict:
    """对外输出：模型 Key 打码 + 媒体压缩参数（数值原样）+ 管理密码是否已设置。"""
    rc = get_runtime_config()
    view: dict = {
        "admin_password_set": bool(rc.get("admin_password_hash") or os.getenv("ADMIN_PASSWORD", "").strip()),
        "media": effective("media"),
        "retrieval": effective("retrieval"),
        "plans": rc.get("plans") or {},
    }
    # 部署档位：下发解析后的生效参数（含档位名），供面板展示当前口径
    from app.core.profiles import effective as profile_effective

    view["deployment"] = profile_effective()
    # rerank 段含 api_key，同样必须打码后再下发前端
    rr = effective("rerank")
    rr["api_key"] = mask_key(rr.get("api_key", ""))
    view["rerank"] = rr
    for kind in ("llm", "embedding", "asr", "vlm"):
        eff = effective(kind)
        eff["api_key"] = mask_key(eff["api_key"])
        eff["configured"] = bool(eff["api_key"] and eff["base_url"] and eff["model"])
        view[kind] = eff
    return view


# ------------------------------------------------------- invalidation hooks --
def on_config_changed() -> None:
    """配置变更后让各服务的热缓存失效。"""
    from app.services.llm.provider import invalidate_provider_cache

    invalidate_provider_cache()


def updated_at() -> float:
    try:
        return _CONFIG_PATH.stat().st_mtime
    except OSError:
        return time.time()
