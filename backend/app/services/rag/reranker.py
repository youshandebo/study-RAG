# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""二阶段精排（Rerank）：把粗排候选池交给 Cross-Encoder 做交叉注意力打分。

设计要点——**解耦"相关度判断"与"可信度赋权"**：

Cross-Encoder 只负责回答"这段文本和问题有多相关"，绝不决定"哪条是权威解法"。
教学场景里学生提问往往口语化，未校准的交叉注意力会偏好口语重合词多的原始
课堂切片，把措辞凝练的教师定版答案挤出首屏。因此定版权威通过**门控阶梯加成**
注入，而非让 reranker 覆盖：

    S_final = S_sem * (1 + beta)   if is_canonical 且 S_sem >= tau
    S_final = S_sem                否则

tau 这个阈值是关键：没有它，一条与问题毫不相干的定版切片也会被强行置顶。

插件化：未配置模型时使用 NoopReranker，直接沿用粗排顺序与 canonical 逻辑，
零依赖即可跑通全部主干测试。
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from app.services.rag.chunker import Chunk

_logger = logging.getLogger("app.rag.reranker")

# 门控参数默认值（管理后台可覆盖）
DEFAULT_TAU = 0.42        # 语义阈值：低于此值不触发权威加成
DEFAULT_BETA = 0.30       # 权威提振幅度
DEFAULT_RECALL_POOL = 18  # 粗排候选池大小（精排从这里挑）


@dataclass
class RerankConfig:
    enabled: bool = False
    tau: float = DEFAULT_TAU
    beta: float = DEFAULT_BETA
    recall_pool: int = DEFAULT_RECALL_POOL


def sigmoid(z: float) -> float:
    """Cross-Encoder 原始 logit → (0,1) 语义置信度。"""
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    ez = math.exp(z)
    return ez / (1.0 + ez)


class BaseReranker:
    """精排协议：输入 query 与 (粗排分, 切片) 列表，输出重排后的同构列表。"""

    name = "base"

    async def rerank(
        self, query: str, scored: list[tuple[float, Chunk]]
    ) -> list[tuple[float, Chunk]]:
        raise NotImplementedError


class NoopReranker(BaseReranker):
    """默认降级：不做任何重排，原样返回粗排结果。

    未安装 onnxruntime / 未放置模型权重 / 显式关闭时的统一出口，
    确保零依赖环境下主干行为与引入精排前完全一致。
    """

    name = "noop"

    async def rerank(
        self, query: str, scored: list[tuple[float, Chunk]]
    ) -> list[tuple[float, Chunk]]:
        return scored


class OnnxReranker(BaseReranker):
    """ONNX Runtime 轻量 Cross-Encoder（如 bge-reranker 量化版）。

    CPU 推理，单例加载。模型缺失时构造失败，由 get_reranker 兜底回 Noop。
    """

    name = "onnx"

    def __init__(self, model_path: str, tokenizer_dir: str = "") -> None:
        import onnxruntime as ort  # 懒加载可选依赖
        from transformers import AutoTokenizer

        self._session = ort.InferenceSession(
            model_path, providers=["CPUExecutionProvider"]
        )
        self._tokenizer = AutoTokenizer.from_pretrained(tokenizer_dir or model_path)
        self._input_names = {i.name for i in self._session.get_inputs()}

    def _logit(self, query: str, text: str) -> float:
        import numpy as np

        enc = self._tokenizer(
            query, text, truncation=True, max_length=512, return_tensors="np"
        )
        feeds = {
            k: np.asarray(v, dtype=np.int64)
            for k, v in enc.items()
            if k in self._input_names
        }
        out = self._session.run(None, feeds)[0]
        return float(np.asarray(out).reshape(-1)[0])

    async def rerank(
        self, query: str, scored: list[tuple[float, Chunk]]
    ) -> list[tuple[float, Chunk]]:
        import asyncio

        # 推理是同步 CPU 密集运算，必须挪出事件循环
        def _run() -> list[tuple[float, Chunk]]:
            rescored: list[tuple[float, Chunk]] = []
            for _coarse, chunk in scored:
                try:
                    z = self._logit(query, f"{chunk.exam_point} {chunk.text}")
                    rescored.append((sigmoid(z), chunk))
                except Exception:
                    rescored.append((0.0, chunk))
            rescored.sort(key=lambda p: p[0], reverse=True)
            return rescored

        return await asyncio.to_thread(_run)


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
    """精排流水线：粗排候选 → Cross-Encoder 打分 → 门控权威加权。

    reranker 为 Noop 时，跳过语义重打与门控，直接返回粗排结果——
    保证"没配模型"与"引入精排前"行为一致。
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
        scored_sem = await self._reranker.rerank(query, pool)
        return apply_authority_gate(scored_sem, self._config.tau, self._config.beta)


_reranker: BaseReranker | None = None


def get_reranker() -> BaseReranker:
    """按配置构造精排器；任何失败都降级为 Noop（不阻断主链路）。"""
    global _reranker
    if _reranker is not None:
        return _reranker
    try:
        import os

        from app.core.runtime_config import effective as cfg_effective

        cfg = cfg_effective("rerank")
        if not cfg.get("enabled"):
            _reranker = NoopReranker()
            return _reranker
        model_path = str(cfg.get("model_path") or os.getenv("RERANK_MODEL_PATH", "")).strip()
        if not model_path:
            _logger.info("rerank 已启用但未配置模型路径，降级 NoopReranker")
            _reranker = NoopReranker()
            return _reranker
        _reranker = OnnxReranker(model_path, str(cfg.get("tokenizer_dir") or ""))
        _logger.info("Cross-Encoder 精排已加载: %s", model_path)
    except Exception as exc:
        _logger.warning("精排器加载失败，降级 NoopReranker（检索仍可用）: %s", exc)
        _reranker = NoopReranker()
    return _reranker


def reset_reranker() -> None:
    """测试与配置热更新用：清掉单例。"""
    global _reranker
    _reranker = None
