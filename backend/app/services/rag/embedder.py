# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""向量化封装：优先 OpenAI 兼容 Embeddings API（管理员面板可配置）；无配置时回落确定性哈希词袋向量（零依赖离线可用）。"""
from __future__ import annotations

import hashlib
import math

from app.core import runtime_config
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


def bm25_tokenize(text: str) -> list[str]:
    """BM25 专用分词：中文按字符 bigram，英文/数字按整词。

    为什么不能复用 `_tokenize`
    -------------------------
    `str.isalnum()` 对汉字返回 True，于是 `_tokenize` 会把
    "梯度下降的学习率衰减策略" 切成**一整个 token**，它与查询 "学习率衰减"
    永不相等——BM25 在中文场景下等于从未匹配过，"关键词通道"只是装饰。

    这也是"混合检索"名存实亡的根因：既然倒排索引对中文不产生任何有效
    posting，那么无论权重怎么调，BM25 贡献恒为 0，Dense 单独决定召回上限。

    bigram 是零依赖的中文检索标准做法：
        "学习率衰减" → 学习 / 习率 / 率衰 / 衰减
    文档只要含相同片段即可命中，不需要词典、不需要新依赖，
    且对未登录词（新术语、人名、公式名）天然友好。

    英文保持整词（bigram 会把 "gradient" 切碎，反而降低区分度）。
    """
    tokens: list[str] = []
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if ch.isascii() and (ch.isalnum() or ch == "_"):
            j = i
            while j < n and text[j].isascii() and (text[j].isalnum() or text[j] == "_"):
                j += 1
            tokens.append(text[i:j].lower())
            i = j
            continue
        if ch.isalnum() and not ch.isascii():
            j = i
            while j < n and text[j].isalnum() and not text[j].isascii():
                j += 1
            seg = text[i:j]
            if len(seg) == 1:
                tokens.append(seg)
            else:
                tokens.extend(seg[k : k + 2] for k in range(len(seg) - 1))
            i = j
            continue
        i += 1
    return tokens


async def embed(text: str) -> list[float]:
    cfg = runtime_config.effective("embedding")
    if cfg["api_key"] and cfg["base_url"] and cfg["model"]:
        import httpx

        async def _call() -> list[float]:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{cfg['base_url'].rstrip('/')}/embeddings",
                    headers={"Authorization": f"Bearer {cfg['api_key']}"},
                    json={"model": cfg["model"], "input": text},
                )
                resp.raise_for_status()
                return resp.json()["data"][0]["embedding"]

        from app.core.security import retry_async

        try:
            return await retry_async(_call, attempts=2, exceptions=(httpx.HTTPError,))
        except Exception as exc:
            import logging

            logging.getLogger("app.rag.embedder").warning(
                "embedding 接口调用失败，降级哈希向量（检索质量下降）: %s", exc
            )
    return _hash_embed(text)


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        shorter = min(len(a), len(b))
        a, b = a[:shorter], b[:shorter]
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)
