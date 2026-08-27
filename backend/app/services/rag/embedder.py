"""向量化封装：优先 OpenAI Embeddings；无 Key 时回落确定性哈希词袋向量（零依赖离线可用）。"""
from __future__ import annotations

import hashlib
import math

from app.core.config import get_settings

_DIM = 256


def _hash_embed(text: str) -> list[float]:
    vec = [0.0] * _DIM
    tokens = _tokenize(text)
    for tok in tokens:
        h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
        idx = h % _DIM
        sign = 1.0 if (h >> 8) % 2 == 0 else -1.0
        vec[idx] += sign
        bigram_ctx = f"#{tok}" if len(tok) > 1 else tok
        vec[int(hashlib.md5(bigram_ctx.encode("utf-8")).hexdigest(), 16) % _DIM] += 0.5 * sign
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _tokenize(text: str) -> list[str]:
    cleaned = "".join(ch.lower() if ch.isascii() else ch for ch in text)
    grams: list[str] = []
    buf = ""
    for ch in cleaned:
        if ch.isalnum() and not ch.isascii():
            buf += ch
        elif ch.isascii() and (ch.isalnum() or ch == "_"):
            buf += ch
        else:
            if buf:
                grams.append(buf)
                buf = ""
    if buf:
        grams.append(buf)
    return [g for g in grams if g]


async def embed(text: str) -> list[float]:
    settings = get_settings()
    if settings.embedding_backend == "openai" and settings.openai_api_key:
        try:
            import httpx

            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{settings.openai_base_url.rstrip('/')}/embeddings",
                    headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                    json={"model": "text-embedding-3-small", "input": text},
                )
                resp.raise_for_status()
                return resp.json()["data"][0]["embedding"]
        except Exception:
            pass
    return _hash_embed(text)


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        shorter = min(len(a), len(b))
        a, b = a[:shorter], b[:shorter]
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)
