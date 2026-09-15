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

向量空间一致性（本模块最容易被忽略的约束）
------------------------------------------
哈希向量是 256 维词袋，真实 embedding 通常是 768/1024/1536 维——两者是**不同的
向量空间**。混进同一集合的后果不是"略有影响"，而是：Qdrant 维度不符直接 400，
或（集合恰为 256 维时）主空间几何拓扑被永久污染且无法区分污染点；检索侧
`cosine()` 又按短维度截断比较，稳定地返回错误结果——比"检索不到"更糟。

因此本模块把"是否降级"作为**一等返回值**（`embed_with_status`），由调用方决定
降级期怎么办：`retriever` 在写入侧跳过向量落库（只进 BM25）、在查询侧跳过 dense
通道。宁可降级到稀疏检索，也不写一个不同空间的向量进去。

仍需注意的边界：本模块的 breaker 状态是**进程内**的，多副本下各副本独立判定降级；
这不影响正确性（每个副本各自决定自己那条链路），但意味着降级恢复时间不完全同步。
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


async def embed_with_status(text: str) -> tuple[list[float], bool]:
    """返回 `(向量, 是否降级)`。

    **为什么必须把"降级"透出来，而不是悄悄返回一个哈希向量**
    ------------------------------------------------------
    哈希向量是 256 维词袋，真实 embedding 通常是 768/1024/1536 维。两者是
    **不同的向量空间**，一旦混进同一个集合：

    - Qdrant 侧：维度不符直接 400（写入失败），或集合恰为 256 维时被当成
      合法向量写入 → 主空间的几何拓扑被永久污染，且不可逆（无法区分哪些点
      是污染点，除非重建集合）；
    - 检索侧：cosine 会按较短维度截断比较，得分完全是无意义的噪声——
      比"检索不到"更糟：它会稳定地返回错误结果。

    所以调用方必须知道"这次向量不可信"：写入路径据此**跳过向量落库**
    （只进 BM25 稀疏索引），查询路径据此**跳过 dense 通道**。降级期检索质量
    下降，但结果是诚实的；污染是永久且隐蔽的。
    """
    cfg = runtime_config.effective("embedding")
    if not (cfg["api_key"] and cfg["base_url"] and cfg["model"]):
        # 未配置接口 = 本部署就以哈希为正式方案（零依赖演示），不算"降级"
        return _hash_embed(text), False

    from app.core.circuit import get_breaker

    breaker = get_breaker(breaker_label(cfg))
    if not breaker.allows():
        # 冷却期内不再向上游发注定失败的请求——这是断路器最实在的收益：
        # 把"每次请求白等两个超时周期"换成"立刻降级"。
        _logger.debug("embedding 通道熔断中，本次直接使用哈希兜底向量")
        return _hash_embed(text), True

    try:
        vector = await _embed_remote(cfg, text)
    except Exception as exc:  # noqa: BLE001 - 上游异常类型不可枚举
        breaker.record_failure(f"{type(exc).__name__}: {exc}")
        _logger.warning(
            "embedding 接口调用失败，降级哈希向量（检索质量下降，且与已存真实向量维度不同）: %s",
            exc,
        )
        return _hash_embed(text), True

    breaker.record_success()
    return vector, False


async def embed(text: str) -> list[float]:
    """只要向量、不关心是否降级的调用方（如纯查询、演示语料灌入）走这里。"""
    vector, _degraded = await embed_with_status(text)
    return vector


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        shorter = min(len(a), len(b))
        a, b = a[:shorter], b[:shorter]
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)
