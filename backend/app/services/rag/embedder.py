# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""向量化封装：优先 OpenAI 兼容 Embeddings API（管理员面板可配置）；无配置时回落确定性哈希词袋向量（零依赖离线可用）。

断路器（与 LLM 侧同款）
------------------------
上游 embedding 接口 502/504 时，原先的路径是"每次调用重试 2 次 → 失败即降级哈希"。
问题在于**每次请求都要重走一遍超时等待**：接口挂了 10 分钟，这 10 分钟里每一次
入库与每一次检索都要白等两个超时周期，请求堆积、网关 504 连锁。

现在接 `core/circuit.CircuitBreaker`（阈值/冷却与 LLM 同源，环境变量可调）：
连续失败跳闸后，冷却期内**直接走哈希兜底**，不再向上游发起注定失败的调用；
冷却结束进入半开，放一次探测，成功即闭合恢复真模型。

一个已知残留（诚实记录，本模块未解决）
--------------------------------------
哈希向量是 256 维，真实 embedding 通常是 768/1024/1536 维。若语料里已存有真实
向量，而降级期间又写入了哈希向量，`cosine()` 会按较短维度截断比较——那部分
相似度不可靠。彻底解法是给向量库标注"空间指纹"并按空间分集合检索（需要动
Qdrant 集合结构），本轮先保证**降级可见**：跳闸与降级都会打 WARNING，
`/admin/ops/circuit` 可直接看到 embedding 通道状态。
"""
from __future__ import annotations

import hashlib
import logging
import math

from app.core import runtime_config
from app.core.config import get_settings

_logger = logging.getLogger("app.rag.embedder")

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


def breaker_label(cfg: dict) -> str:
    """按 (端点 + 模型) 隔离：换了模型就是换了故障域，不该共用一份失败计数。"""
    base = str(cfg.get("base_url") or "").rstrip("/")
    model = str(cfg.get("model") or "")
    return f"embedding:{base}#{model}"


async def _embed_remote(cfg: dict, text: str) -> list[float]:
    """真实接口调用（拆出来是为了让测试能替换它而不必起 HTTP 服务）。"""
    import httpx

    from app.core.security import retry_async

    async def _call() -> list[float]:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{cfg['base_url'].rstrip('/')}/embeddings",
                headers={"Authorization": f"Bearer {cfg['api_key']}"},
                json={"model": cfg["model"], "input": text},
            )
            resp.raise_for_status()
            return resp.json()["data"][0]["embedding"]

    return await retry_async(_call, attempts=2, exceptions=(httpx.HTTPError,))


async def embed(text: str) -> list[float]:
    cfg = runtime_config.effective("embedding")
    if not (cfg["api_key"] and cfg["base_url"] and cfg["model"]):
        return _hash_embed(text)

    from app.core.circuit import get_breaker

    breaker = get_breaker(breaker_label(cfg))
    if not breaker.allows():
        # 冷却期内不再向上游发注定失败的请求——这是断路器最实在的收益：
        # 把"每次请求白等两个超时周期"换成"立刻降级"。
        _logger.debug("embedding 通道熔断中，本次直接使用哈希兜底向量")
        return _hash_embed(text)

    try:
        vector = await _embed_remote(cfg, text)
    except Exception as exc:  # noqa: BLE001 - 上游异常类型不可枚举
        breaker.record_failure(f"{type(exc).__name__}: {exc}")
        _logger.warning(
            "embedding 接口调用失败，降级哈希向量（检索质量下降，且与已存真实向量维度不同）: %s",
            exc,
        )
        return _hash_embed(text)

    breaker.record_success()
    return vector


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        shorter = min(len(a), len(b))
        a, b = a[:shorter], b[:shorter]
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)
