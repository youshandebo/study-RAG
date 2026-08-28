"""管理员面板接口：JWT 登录认证 + 四类模型(LLM/嵌入/ASR/VLM)运行时配置 + 连通性测试 + 用量统计。

- 认证：POST /admin/login 换取 HS256 签名 JWT（24h 有效，无状态可校验），
  后续请求必须携带 X-Admin-Token 或 Authorization: Bearer。
- 配置语义：GET 返回打码视图；PUT 提交掩码值 = 保持不变，提交空串 = 回落 .env 默认。
- 配置写入前做 SSRF 校验：base_url 不允许指向内网 / 云元数据端点。
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import time
import wave

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

import app.core.runtime_config as runtime_config
import app.db.relational as repo
from app.core.security import assert_safe_url, jwt_sign, jwt_verify

router = APIRouter()

_TOKEN_TTL = 24 * 3600
_fail_count = {"n": 0, "last": 0.0}
# 登录暴力破解防护之外，管理接口本身也限流（防脚本扫端）
from app.core.security import SlidingWindowLimiter  # noqa: E402

_admin_limiter = SlidingWindowLimiter(max_events=60, window_seconds=60)


def _check_token(token: str) -> bool:
    return bool(token) and jwt_verify(token) is not None


async def require_admin(
    request: Request,
    x_admin_token: str = Header(default=""),
    authorization: str = Header(default=""),
) -> str:
    client_ip = request.client.host if request.client else "unknown"
    if not _admin_limiter.check(f"admin-api:{client_ip}"):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    token = x_admin_token or (authorization[7:] if authorization.startswith("Bearer ") else "")
    if not token or not _check_token(token):
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return token


# ------------------------------------------------------------------- models --
class LoginBody(BaseModel):
    password: str


class PasswordBody(BaseModel):
    old_password: str
    new_password: str


@router.post("/admin/login")
async def admin_login(request: Request, body: LoginBody):
    # 简单防爆破：连续失败递增冷却
    if _fail_count["n"] >= 5 and time.time() - _fail_count["last"] < min(8.0, _fail_count["n"] * 1.6):
        raise HTTPException(status_code=429, detail="尝试过于频繁，请稍后再试")
    client_ip = request.client.host if request.client else "unknown"
    if not _admin_limiter.check(f"admin-login:{client_ip}"):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    await asyncio.sleep(0.25)
    if not runtime_config.verify_admin_password(body.password):
        _fail_count["n"] += 1
        _fail_count["last"] = time.time()
        raise HTTPException(status_code=401, detail="管理员密码不正确")
    _fail_count["n"] = 0
    token = jwt_sign({"role": "admin"}, _TOKEN_TTL)
    return {
        "token": token,
        "expires_in": _TOKEN_TTL,
        "password_default": not (
            runtime_config.get_runtime_config().get("admin_password_hash") or os.getenv("ADMIN_PASSWORD", "").strip()
        ),
    }


@router.post("/admin/logout")
async def admin_logout(token: str = Depends(require_admin)):
    # JWT 为无状态凭证，客户端删除即可；预留服务端吊销位
    return {"ok": True}


@router.get("/admin/config")
async def get_admin_config(_: str = Depends(require_admin)):
    view = runtime_config.masked_view()
    view["model_tracks"] = available_tracks()
    return view


def available_tracks() -> list[dict]:
    """当前可用的多模型比对轨道（含面板配置的主模型）。"""
    from app.services.llm.provider import available_model_keys

    display = {"main": "主模型(面板)", "gpt": "OpenAI", "claude": "Claude", "qwen": "Qwen-Max", "deepseek": "DeepSeek"}
    return [{"key": k, "name": display.get(k, k)} for k in available_model_keys()]


@router.put("/admin/config")
async def put_admin_config(payload: dict, _: str = Depends(require_admin)):
    patch: dict = {}
    for kind in ("llm", "embedding", "asr", "vlm"):
        section = payload.get(kind)
        if not isinstance(section, dict):
            continue
        out: dict = {}
        for key, val in section.items():
            v = str(val or "").strip()
            if runtime_config.is_masked(v):
                continue  # 掩码 = 保持现有存储不动
            if key == "base_url" and v:
                try:
                    assert_safe_url(v)
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=f"{kind} base_url 不安全：{exc}") from None
            out[key] = v
        patch[kind] = out
    # 媒体压缩参数：数值校验在 save_runtime_config 内完成
    media = payload.get("media")
    if isinstance(media, dict):
        patch["media"] = {k: str(v) for k, v in media.items() if str(v or "").strip() != ""}
    runtime_config.save_runtime_config(patch)
    return runtime_config.masked_view()


class SectionReset(BaseModel):
    kind: str


@router.post("/admin/config/reset")
async def reset_section(body: SectionReset, _: str = Depends(require_admin)):
    if body.kind not in ("llm", "embedding", "asr", "vlm"):
        raise HTTPException(status_code=400, detail="未知模型类型")
    blank = {"provider": "", "base_url": "", "api_key": "", "model": ""}
    runtime_config.save_runtime_config({body.kind: blank})
    return runtime_config.masked_view()


@router.post("/admin/password")
async def change_password(body: PasswordBody, _: str = Depends(require_admin)):
    new = body.new_password.strip()
    if len(new) < 6:
        raise HTTPException(status_code=400, detail="新密码至少 6 位")
    if not runtime_config.verify_admin_password(body.old_password):
        raise HTTPException(status_code=401, detail="原密码不正确")
    runtime_config.set_admin_password(new)
    return {"ok": True, "message": "管理员密码已更新"}


@router.post("/admin/config/test")
async def test_config(kind: str, _: str = Depends(require_admin)):
    """按模型类型实测连通性：llm / embedding / asr / vlm。"""
    cfg = runtime_config.effective(kind)
    if kind == "asr" and not (cfg["api_key"] and cfg["base_url"] and cfg["model"]):
        whisper_ok = _whisper_available()
        return {
            "kind": kind,
            "ok": True,
            "mode": "local-whisper" if whisper_ok else "demo",
            "message": f"未配置远程 ASR；本地 Whisper {'可用' if whisper_ok else '未安装，将使用演示转录兜底'}",
        }
    if not (cfg["api_key"] and cfg["base_url"] and cfg["model"]):
        return {
            "kind": kind,
            "ok": False,
            "mode": "demo",
            "message": "该模型尚未配置完整（base_url / api_key / model 有缺项），当前使用内置演示引擎兜底",
        }
    try:
        if kind == "llm":
            message = await _test_llm(cfg)
        elif kind == "embedding":
            vec = await _test_embedding(cfg)
            message = f"返回向量维度 {len(vec)}"
        elif kind == "vlm":
            message = await _test_vlm(cfg)
        else:
            message = await _test_asr(cfg)
        return {"kind": kind, "ok": True, "mode": cfg["model"], "message": message}
    except Exception as exc:  # noqa: BLE001 —— 面板需要展示任何失败原因
        return {"kind": kind, "ok": False, "mode": cfg["model"], "message": f"连接失败：{exc}"}


def _whisper_available() -> bool:
    try:
        import faster_whisper  # noqa: F401

        return True
    except Exception:
        return False


async def _chat_completion(cfg: dict, content: list | None, max_tokens: int = 8) -> str:
    """统一 chat 探测：自动区分 OpenAI 兼容 / Anthropic 协议。"""
    import httpx

    messages = content or [{"type": "text", "text": "回复 OK"}]
    headers: dict
    url: str
    payload: dict
    if cfg.get("provider") == "anthropic":
        headers = {"x-api-key": cfg["api_key"], "anthropic-version": "2023-06-01"}
        url = f"{cfg['base_url'].rstrip('/')}/v1/messages"
        payload = {"model": cfg["model"], "max_tokens": max_tokens, "messages": [{"role": "user", "content": messages}]}
    else:
        headers = {"Authorization": f"Bearer {cfg['api_key']}"}
        url = f"{cfg['base_url'].rstrip('/')}/chat/completions"
        payload = {
            "model": cfg["model"],
            "messages": [{"role": "user", "content": messages}],
            "max_tokens": max_tokens,
            "stream": False,
        }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    if "choices" in data:
        msg = data["choices"][0].get("message", {}).get("content", "")
        return (msg or "")[:60]
    text = data.get("content") or ""
    if isinstance(text, list):
        text = "".join(t.get("text", "") for t in text)
    return str(text)[:60]


async def _test_llm(cfg: dict) -> str:
    reply = await _chat_completion(cfg)
    return f"连接成功 · 模型回复「{reply or '(空)'}」"


async def _test_embedding(cfg: dict) -> list[float]:
    import httpx

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{cfg['base_url'].rstrip('/')}/embeddings",
            headers={"Authorization": f"Bearer {cfg['api_key']}"},
            json={"model": cfg["model"], "input": "连通性测试"},
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]


async def _test_vlm(cfg: dict) -> str:
    # 纯文本探测：识图端点同样应接受文本消息，验证地址与鉴权即可
    reply = await _chat_completion(cfg, [{"type": "text", "text": "回复 OK"}])
    return f"连接成功 · 模型回复「{reply or '(空)'}」"


def _tiny_wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 2400)  # 0.3 秒静音
    return buf.getvalue()


async def _test_asr(cfg: dict) -> str:
    import httpx

    audio = _tiny_wav()
    async with httpx.AsyncClient(timeout=40) as client:
        resp = await client.post(
            f"{cfg['base_url'].rstrip('/')}/audio/transcriptions",
            headers={"Authorization": f"Bearer {cfg['api_key']}"},
            files={"file": ("probe.wav", audio, "audio/wav")},
            data={"model": cfg["model"]},
        )
        resp.raise_for_status()
        text = str(resp.json().get("text", ""))
        return f"连接成功 · 静音样本转录结果「{text[:40]}」"


# -------------------------------------------------------------------- stats --
@router.get("/admin/stats")
async def admin_stats(_: str = Depends(require_admin)):
    retriever = await get_retriever_safe()
    chunks = await retriever.all_chunks() if retriever else []
    assets_total = sum(len(v) for v in repo._memory_assets.values())
    custom = runtime_config.get_runtime_config()
    return {
        "sessions": len(await repo.list_sessions()),
        "assets": assets_total,
        "chunks": len(chunks),
        "seed_chunks": len([c for c in chunks if c.audio_id.startswith("lec-")]) if chunks else 0,
        "uploaded_chunks": len(chunks) - len([c for c in chunks if c.audio_id.startswith("lec-")]) if chunks else 0,
        "mock_mode": get_settings_snapshot().mock_mode,
        "tracks": available_tracks(),
        "custom_sections": {
            k: bool(v.get("api_key")) for k, v in custom.items() if isinstance(v, dict)
        },
        "runtime_overridden": bool(runtime_config.updated_at()),
    }


def get_settings_snapshot():
    from app.core.config import get_settings

    return get_settings()


async def get_retriever_safe():
    try:
        from app.services.rag.retriever import get_retriever

        return await get_retriever()
    except Exception:
        return None
