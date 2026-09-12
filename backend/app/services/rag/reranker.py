# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""二阶段精排（Rerank）：把粗排候选池交给**远程 Rerank API** 打分。

为什么是 API 而不是本地 Cross-Encoder
------------------------------------
本项目的部署形态是小型 VPS。本地 ONNX Cross-Encoder 需要几百 MB 权重 +
常驻内存 + CPU 推理，与"单机小内存 + 多租户"的运营目标直接冲突。重排本身
是**可选增强**（不装也能跑），因此它必须廉价、可关、可降级——这三点恰好是
远程 API 的强项，是本地推理的弱项。故本模块只保留 API 驱动实现。

设计要点——**解耦"相关度判断"与"可信度赋权"**：

Rerank API 只负责回答"这段文本和问题有多相关"，绝不决定"哪条是权威解法"。
教学场景里学生提问往往口语化，未校准的交叉注意力会偏好口语重合词多的原始
课堂切片，把措辞凝练的教师定版答案挤出首屏。因此定版权威通过**门控阶梯加成**
注入，而非让 reranker 覆盖：

    S_final = S_sem * (1 + beta)   if is_canonical 且 S_sem >= tau
    S_final = S_sem                否则

tau 这个阈值是关键：没有它，一条与问题毫不相干的定版切片也会被强行置顶。

插件化：未配置 API 时使用 NoopReranker，直接沿用粗排顺序与 canonical 逻辑，
零外部依赖即可跑通全部主干测试。
"""
from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass

from app.services.rag.chunker import Chunk

_logger = logging.getLogger("app.rag.reranker")

# 门控参数默认值（管理后台可覆盖）
DEFAULT_TAU = 0.42        # 语义阈值：低于此值不触发权威加成
DEFAULT_BETA = 0.30       # 权威提振幅度
DEFAULT_RECALL_POOL = 18  # 粗排候选池大小（精排从这里挑）

# 各厂商 Rerank 接口协议（路径 + 请求体形状不同，响应形状统一为 {results:[{index,relevance_score}]}）
DEFAULT_PROTOCOL = "jina"
DEFAULT_TIMEOUT_S = 8.0   # 小 VPS 上重排是"锦上添花"，宁可降级也不能拖慢首字
DEFAULT_API_MODEL: dict[str, str] = {
    "jina": "jina-reranker-v2-base-multilingual",
    "siliconflow": "BAAI/bge-reranker-v2-m3",
    "cohere": "rerank-multilingual-v3.0",
}
DEFAULT_API_BASE: dict[str, str] = {
    "jina": "https://api.jina.ai/v1/rerank",
    "siliconflow": "https://api.siliconflow.cn/v1/rerank",
    "cohere": "https://api.cohere.com/v2/rerank",
}


@dataclass
class RerankConfig:
    enabled: bool = False
    tau: float = DEFAULT_TAU
    beta: float = DEFAULT_BETA
    recall_pool: int = DEFAULT_RECALL_POOL


def sigmoid(z: float) -> float:
    """线性相关分 → (0,1) 语义置信度。

    远程 API（Jina / SiliconFlow / Cohere）返回的 relevance_score 已在 [0,1]，
    无需再走 sigmoid；本函数保留给"返回原始 logit"的自建端点使用，同时也是
    离线标定脚本与历史数据的兼容口径。
    """
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    ez = math.exp(z)
    return ez / (1.0 + ez)


def _normalize_scores(raw_scores: list[float]) -> list[float]:
    """把任意量纲的打分映射到 [0,1]。

    三种情形：
    - 全在 [0,1] 且不是"原始 logit 的巧合" → 原样使用（API 主路径）；
    - 出现负值或 >1 → 判定为 logit，逐条 sigmoid；
    - 空列表 → 原样返回。
    """
    if not raw_scores:
        return []
    if all(0.0 <= s <= 1.0 for s in raw_scores):
        return list(raw_scores)
    return [sigmoid(s) for s in raw_scores]


class BaseReranker:
    """精排协议：输入 query 与 (粗排分, 切片) 列表，输出重排后的同构列表。"""

    name = "base"

    async def rerank(
        self, query: str, scored: list[tuple[float, Chunk]]
    ) -> list[tuple[float, Chunk]]:
        raise NotImplementedError


class NoopReranker(BaseReranker):
    """默认降级：不做任何重排，原样返回粗排结果。

    未配置 API / 未启用 / 请求失败时的统一出口，确保无外部依赖环境下
    主干行为与引入精排前完全一致。
    """

    name = "noop"

    async def rerank(
        self, query: str, scored: list[tuple[float, Chunk]]
    ) -> list[tuple[float, Chunk]]:
        return scored


def build_payload(protocol: str, model: str, query: str, documents: list[str], top_n: int) -> dict:
    """按厂商协议组装请求体（纯函数，便于单测钉住契约）。"""
    if protocol == "cohere":
        # Cohere v2：documents 为字符串列表，另有 top_n
        return {"model": model, "query": query, "documents": documents, "top_n": top_n}
    # Jina / SiliconFlow 同构：documents 为 [{"text": ...}]，带 top_n
    return {
        "model": model,
        "query": query,
        "documents": [{"text": d} for d in documents],
        "top_n": top_n,
    }


def parse_rerank_response(data: dict) -> list[tuple[int, float]]:
    """解析响应为 (原文档下标, 得分) 列表；兼容三厂商的字段命名差异。

    统一形状：{"results": [{"index": i, "relevance_score": s}, ...]}
    Cohere v2 用 "relevance_score"；部分网关用 "score" / "similarity"。
    """
    results = data.get("results")
    if results is None:
        results = data.get("data")
    if not isinstance(results, list):
        raise ValueError("rerank 响应缺少 results 数组")

    parsed: list[tuple[int, float]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        idx = item.get("index")
        if idx is None:
            idx = item.get("document_index")
        score = None
        for key in ("relevance_score", "score", "similarity", "relevance"):
            if item.get(key) is not None:
                score = item[key]
                break
        if idx is None or score is None:
            continue
        try:
            parsed.append((int(idx), float(score)))
        except (TypeError, ValueError):
            continue
    if not parsed:
        raise ValueError("rerank 响应中未解析出任何 (index, score)")
    return parsed


class ApiReranker(BaseReranker):
    """远程 Rerank API（Jina / SiliconFlow / Cohere 及任意同构网关）。

    轻量：仅依赖 `httpx`，无常驻内存模型、无本地推理、无几百 MB 权重。
    单次调用失败一律向上抛，由 `RerankPipeline` 降级为粗排顺序——
    重排绝不能成为检索链路的单点故障。
    """

    name = "api"

    def __init__(
        self,
        api_base: str,
        api_key: str,
        model: str,
        protocol: str = DEFAULT_PROTOCOL,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._api_base = api_base.strip()
        self._api_key = api_key.strip()
        self._model = model.strip()
        self._protocol = (protocol or DEFAULT_PROTOCOL).strip().lower()
        self._timeout = float(timeout)
        if not self._api_base:
            raise ValueError("ApiReranker 需要 api_base")

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def rerank(
        self, query: str, scored: list[tuple[float, Chunk]]
    ) -> list[tuple[float, Chunk]]:
        if not scored:
            return scored
        import httpx

        documents = [f"{c.exam_point} {c.text}".strip() for _, c in scored]
        payload = build_payload(self._protocol, self._model, query, documents, len(documents))

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(self._api_base, json=payload, headers=self._headers())
            resp.raise_for_status()
            data = resp.json()

        pairs = parse_rerank_response(data)
        # 未在 results 中出现的下标回填为 0 分，保证候选不丢
        score_by_idx = {i: s for i, s in pairs}
        raw = [score_by_idx.get(i, 0.0) for i in range(len(scored))]
        norm = _normalize_scores(raw)

        rescored: list[tuple[float, Chunk]] = [
            (norm[i], scored[i][1]) for i in range(len(scored))
        ]
        # 稳定排序：同分时保持粗排相对次序（python sort 本身稳定）
        rescored.sort(key=lambda p: p[0], reverse=True)
        return rescored


def apply_authority_gate(
    scored: list[tuple[float, Chunk]], tau: float, beta: float
) -> list[tuple[float, Chunk]]:
    """门控权威加权：仅对语义达标（S_sem >= tau）的定版切片加成。

    加成后重新排序——定版切片最多获得 (1+beta) 倍提振，不足以让一条
    语义很弱的切片越过语义明显更强的切片。
    """
    out: list[tuple[float, Chunk]] = []
    for s_sem, chunk in scored:
        if chunk.is_canonical and s_sem >= tau:
            out.append((s_sem * (1.0 + beta), chunk))
        else:
            out.append((s_sem, chunk))
    out.sort(key=lambda p: p[0], reverse=True)
    return out


class RerankPipeline:
    """精排流水线：粗排候选 → 远程 API 打分 → 门控权威加权。

    reranker 为 Noop 时，跳过语义重打与门控，直接返回粗排结果——
    保证"没配 API"与"引入精排前"行为一致。

    网络抖动 / 限流 / 超时：**捕获后回退粗排顺序**，不抛给上层。
    这是有意的取舍——重排是加分项，宁可这一跳不生效，也不能让整个问答失败。
    """

    def __init__(self, reranker: BaseReranker, config: RerankConfig) -> None:
        self._reranker = reranker
        self._config = config

    @property
    def active(self) -> bool:
        return self._config.enabled and self._reranker.name != "noop"

    @property
    def recall_pool(self) -> int:
        return max(1, int(self._config.recall_pool))

    async def run(
        self, query: str, scored: list[tuple[float, Chunk]]
    ) -> list[tuple[float, Chunk]]:
        if not self.active or not scored:
            return scored
        pool = scored[: max(1, self._config.recall_pool)]
        try:
            scored_sem = await self._reranker.rerank(query, pool)
        except Exception as exc:  # 网络/限流/协议异常 → 静默降级
            _logger.warning("重排 API 调用失败，回退粗排顺序: %s", exc)
            return scored
        return apply_authority_gate(scored_sem, self._config.tau, self._config.beta)


_reranker: BaseReranker | None = None


def get_reranker() -> BaseReranker:
    """按配置构造精排器；任何失败都降级为 Noop（不阻断主链路）。

    优先级：面板 rerank.api_base > 环境变量 RERANK_API_BASE / RERANK_API_KEY。
    """
    global _reranker
    if _reranker is not None:
        return _reranker
    try:
        from app.core.runtime_config import effective as cfg_effective

        cfg = cfg_effective("rerank")
        if not cfg.get("enabled"):
            _reranker = NoopReranker()
            return _reranker

        api_base = str(cfg.get("api_base") or os.getenv("RERANK_API_BASE", "")).strip()
        if not api_base:
            _logger.info("rerank 已启用但未配置 api_base，降级 NoopReranker")
            _reranker = NoopReranker()
            return _reranker

        protocol = str(cfg.get("protocol") or "").strip().lower() or DEFAULT_PROTOCOL
        api_key = str(cfg.get("api_key") or "").strip() or os.getenv("RERANK_API_KEY", "").strip()
        model = str(cfg.get("api_model") or "").strip() or DEFAULT_API_MODEL.get(
            protocol, DEFAULT_API_MODEL[DEFAULT_PROTOCOL]
        )
        _reranker = ApiReranker(
            api_base=api_base,
            api_key=api_key,
            model=model,
            protocol=protocol,
            timeout=float(cfg.get("timeout") or DEFAULT_TIMEOUT_S),
        )
        _logger.info("远程 Rerank 已启用: protocol=%s model=%s", protocol, model)
    except Exception as exc:
        _logger.warning("精排器加载失败，降级 NoopReranker（检索仍可用）: %s", exc)
        _reranker = NoopReranker()
    return _reranker


def reset_reranker() -> None:
    """测试与配置热更新用：清掉单例。"""
    global _reranker
    _reranker = None


def default_api_base(protocol: str) -> str:
    return DEFAULT_API_BASE.get(protocol, DEFAULT_API_BASE[DEFAULT_PROTOCOL])


def default_api_model(protocol: str) -> str:
    return DEFAULT_API_MODEL.get(protocol, DEFAULT_API_MODEL[DEFAULT_PROTOCOL])
