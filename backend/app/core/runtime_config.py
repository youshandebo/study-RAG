"""运行时模型配置：管理员面板驱动的持久化配置层。

- 配置落盘 JSON（backend/data/runtime_config.json），修改即时生效、重启保留。
- 各字段留空时自动回落 .env 环境变量默认值，保证"环境变量首次部署 / 面板二次调整"两级配置语义。
- API Key 读取时打码返回，仅当提交值非掩码时才覆盖存储。
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import secrets
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
        "llm": {"provider": "", "base_url": "", "api_key": "", "model": ""},
        "embedding": {"base_url": "", "api_key": "", "model": ""},
        "asr": {"base_url": "", "api_key": "", "model": ""},
        "vlm": {"base_url": "", "api_key": "", "model": ""},
        # 媒体压缩参数（数值以字符串形式存储，effective() 负责转型）
        "media": {"image_quality": "", "image_max_edge": "", "audio_bitrate": "", "audio_max_mb": ""},
    }


_MEDIA_KEYS = ("image_quality", "image_max_edge", "audio_bitrate", "audio_max_mb")


def _normalize(raw: dict | None) -> dict:
    """补齐缺失键，丢弃未知键；只保留字符串字段。"""
    cfg = _blank()
    if not isinstance(raw, dict):
        return cfg
    cfg["admin_password_hash"] = str(raw.get("admin_password_hash") or "")
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
    for section in ("llm", "embedding", "asr", "vlm", "media"):
        src = patch.get(section)
        if not isinstance(src, dict):
            continue
        target = current.setdefault(section, {})
        for key in list(target):
            if key not in src:
                continue
            val = src[key]
            if section == "media":
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
def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(8)
    digest = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return f"{salt}${digest}"


def verify_admin_password(password: str) -> bool:
    stored = get_runtime_config().get("admin_password_hash", "")
    if not stored:
        stored = os.getenv("ADMIN_PASSWORD", "").strip()
    if not stored:
        return password == "admin123"  # 首次部署默认口令，登录后请修改
    if "$" in stored:
        salt, _ = stored.split("$", 1)
        return hash_password(password, salt) == stored
    return password == stored  # 兼容环境变量直接写明文口令


def set_admin_password(password: str) -> None:
    with _lock:
        current = _load_from_disk()
        current["admin_password_hash"] = hash_password(password)
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_CONFIG_PATH)
        globals()["_cache"] = current
        globals()["_cache_mtime"] = _CONFIG_PATH.stat().st_mtime


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
    }
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
