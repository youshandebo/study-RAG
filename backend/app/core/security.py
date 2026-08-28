"""安全基础设施：SSRF 防护 / 滑动窗口限流 / 指数退避重试 / 极简 HS256 JWT。

全部基于标准库实现，零新增依赖。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import random
import socket
import time
from collections import defaultdict, deque
from urllib.parse import urlparse

# ------------------------------------------------------------------ SSRF ----

# 官方大模型域名白名单（管理员面板/环境变量可扩展私有网关）
OFFICIAL_LLM_HOSTS = {
    "api.openai.com", "api.anthropic.com", "api.deepseek.com",
    "dashscope.aliyuncs.com", "open.bigmodel.cn", "api.moonshot.cn",
    "api.siliconflow.cn", "api.groq.com", "api.jina.ai", "api.x.ai",
    "openrouter.ai", "api.together.xyz", "api.mistral.ai",
}

BLOCKED_METADATA_HOSTS = {"169.254.169.254", "metadata.google.internal"}


def _is_private_ip(host: str) -> bool:
    try:
        addr = ipaddress.ip_address(host)
        return addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved
    except ValueError:
        return False


def _resolves_private(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return True  # 解析失败按不安全处理
    return any(_is_private_ip(info[4][0]) for info in infos)


def assert_safe_url(url: str, *, allow_private: bool | None = None) -> None:
    """校验外部请求目标：协议、内网地址与云元数据端点一律拒绝。

    allow_private 缺省时读取环境变量 ALLOW_PRIVATE_LLM_HOSTS=1，
    供自部署私有网关显式放开（仅建议内网部署开启）。
    """
    if allow_private is None:
        allow_private = os.getenv("ALLOW_PRIVATE_LLM_HOSTS", "").strip() == "1"
    parsed = urlparse(url or "")
    if parsed.scheme not in ("https", "http"):
        raise ValueError(f"不允许的协议：{parsed.scheme or '(空)'}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("URL 缺少主机名")
    if allow_private:
        return
    if host in BLOCKED_METADATA_HOSTS or _is_private_ip(host) or _resolves_private(host):
        raise ValueError(f"禁止请求内网/元数据地址：{host}")


def is_official_llm_host(url: str) -> bool:
    return (urlparse(url or "").hostname or "").lower() in OFFICIAL_LLM_HOSTS


# ---------------------------------------------------------- rate limiting --

class SlidingWindowLimiter:
    """进程内滑动窗口限流器（按 key 维度）。多副本部署应换 Redis 实现。"""

    def __init__(self, max_events: int, window_seconds: float) -> None:
        self.max_events = max_events
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> bool:
        """放行返回 True 并记账；超限返回 False。"""
        now = time.time()
        dq = self._hits[key]
        while dq and now - dq[0] > self.window:
            dq.popleft()
        if len(dq) >= self.max_events:
            return False
        dq.append(now)
        return True

    def retry_after(self, key: str) -> int:
        dq = self._hits.get(key)
        if not dq:
            return 0
        return max(1, int(self.window - (time.time() - dq[0])) + 1)


# --------------------------------------------------------------- backoff ----

async def retry_async(coro_factory, *, attempts: int = 3, base_delay: float = 0.6,
                      exceptions: tuple = (Exception,), on_give_up=None):
    """指数退避重试：0.6s → 1.2s → 2.4s（±20% 抖动）。最终失败时抛出最后异常。"""
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return await coro_factory()
        except exceptions as exc:  # noqa: PERF203
            last_exc = exc
            if i < attempts - 1:
                await asyncio.sleep(base_delay * (2**i) * (0.8 + random.random() * 0.4))
    if on_give_up:
        on_give_up(last_exc)
    assert last_exc is not None
    raise last_exc


# ------------------------------------------------------------------ JWT -----
_SECRET = os.getenv("ADMIN_JWT_SECRET", "") or os.getenv("ADMIN_PASSWORD", "") or "studay-rag-dev-secret"


def jwt_sign(payload: dict, ttl_seconds: int) -> str:
    """HS256 极简 JWT：header.payload.signature（base64url，无第三方依赖）。"""
    header = {"alg": "HS256", "typ": "JWT"}
    body = {**payload, "exp": int(time.time()) + ttl_seconds}
    seg = lambda d: base64.urlsafe_b64encode(  # noqa: E731
        json.dumps(d, separators=(",", ":"), ensure_ascii=False).encode()
    ).rstrip(b"=").decode()
    signing = f"{seg(header)}.{seg(body)}"
    sig = hmac.new(_SECRET.encode(), signing.encode(), hashlib.sha256).hexdigest()
    return f"{signing}.{sig}"


def jwt_verify(token: str) -> dict | None:
    """校验签名与有效期；合法返回 payload，否则 None。"""
    try:
        signing, sig = token.rsplit(".", 1)
        expect = hmac.new(_SECRET.encode(), signing.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expect):
            return None
        b64 = signing.split(".", 1)[1]
        body = json.loads(base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)))
        if int(body.get("exp", 0)) < time.time():
            return None
        return body
    except Exception:
        return None
