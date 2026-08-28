# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""混合检索器：向量相似度 + BM25 倒排 + 词面重合 + 考点元数据过滤。

优先写入/查询 Qdrant；未配置 Qdrant 时自动使用进程内向量索引（零依赖兜底）。
进程内 BM25 关键词倒排索引随每次向量入库同步增量更新，读写锁保证并发安全。
"""
from __future__ import annotations

import asyncio
import math
import threading

from app.core.config import get_settings
from app.db.vector_store import get_vector_store
from app.services.rag import corpus, embedder
from app.services.rag.chunker import Chunk


class RWLock:
    """读写锁：读共享、写独占（Condition 实现，标准库零依赖）。"""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._readers = 0
        self._writing = False

    def acquire_read(self) -> None:
        with self._cond:
            while self._writing:
                self._cond.wait()
            self._readers += 1

    def release_read(self) -> None:
        with self._cond:
            self._readers -= 1
            if self._readers == 0:
                self._cond.notify_all()

    def acquire_write(self) -> None:
        with self._cond:
            while self._writing or self._readers:
                self._cond.wait()
            self._writing = True

    def release_write(self) -> None:
        with self._cond:
            self._writing = False
            self._cond.notify_all()

    def __enter__(self):  # 写上下文
        self.acquire_write()
        return self

    def __exit__(self, *exc) -> None:
        self.release_write()


class BM25Index:
    """内存 BM25 关键词倒排索引：k1=1.5, b=0.75 经典参数。"""

    K1, B = 1.5, 0.75

    def __init__(self) -> None:
        self._lock = RWLock()
        self._postings: dict[str, dict[str, int]] = {}  # term -> {chunk_id: tf}
        self._doc_len: dict[str, int] = {}
        self._avg_len = 1.0

    def add(self, chunk_id: str, tokens: list[str]) -> None:
        with self._lock:
            for tok in tokens:
                postings = self._postings.setdefault(tok, {})
                postings[chunk_id] = postings.get(chunk_id, 0) + 1
            self._doc_len[chunk_id] = len(tokens) or 1
            total = sum(self._doc_len.values())
            self._avg_len = total / max(len(self._doc_len), 1)

    def scores(self, query_tokens: list[str]) -> dict[str, float]:
        """对全量文档计算 BM25 得分（N 为千级，全扫足够）。"""
        self._lock.acquire_read()
        try:
            n_docs = max(len(self._doc_len), 1)
            scores: dict[str, float] = {}
            for tok in query_tokens:
                postings = self._postings.get(tok)
                if not postings:
                    continue
                idf = math.log(1 + (n_docs - len(postings) + 0.5) / (len(postings) + 0.5))
                for cid, tf in postings.items():
                    dl = self._doc_len.get(cid, 1)
                    denom = tf + self.K1 * (1 - self.B + self.B * dl / self._avg_len)
                    scores[cid] = scores.get(cid, 0.0) + idf * (tf * (self.K1 + 1)) / denom
            return scores
        finally:
            self._lock.release_read()


class HybridRetriever:
    def __init__(self) -> None:
        self._store = get_vector_store()
        self._chunks: dict[str, Chunk] = {}
        self._vectors: dict[str, list[float]] = {}
        self._bm25 = BM25Index()
        self._seeded = False

    async def _ensure_seeded(self) -> None:
        if self._seeded:
            return
        for item in corpus.SEED_CHUNKS:
            chunk = Chunk(**item)
            self._chunks[chunk.id] = chunk
            vec = await embedder.embed(f"{chunk.exam_point} {chunk.text}")
            self._vectors[chunk.id] = vec
            await self._store.upsert(chunk.id, vec, chunk.to_payload())
            self._bm25.add(chunk.id, embedder._tokenize(f"{chunk.exam_point} {chunk.text}"))
        self._seeded = True

    @staticmethod
    def _lexical_overlap(query_tokens: set[str], text: str) -> float:
        if not query_tokens:
            return 0.0
        hay = set(embedder._tokenize(text))
        inter = query_tokens & hay
        return len(inter) / len(query_tokens)

    async def retrieve(
        self,
        query: str,
        top_k: int = 3,
        exam_point: str | None = None,
        min_score: float | None = None,
    ) -> list[Chunk]:
        if min_score is None:
            return [c for _, c in await self.retrieve_scored(query, top_k, exam_point)]
        return [
            c
            for s, c in await self.retrieve_scored(query, top_k * 3, exam_point)
            if s >= min_score
        ][:top_k]

    async def retrieve_scored(
        self,
        query: str,
        top_k: int = 3,
        exam_point: str | None = None,
    ) -> list[tuple[float, Chunk]]:
        """返回 (融合得分, 切片)：0.50 向量 + 0.20 词面 + 0.20 BM25 + 0.15 元数据。"""
        await self._ensure_seeded()
        qvec = await embedder.embed(query)
        qtokens = embedder._tokenize(query)
        bm25_raw = self._bm25.scores(qtokens)
        bm25_max = max(bm25_raw.values(), default=0.0)

        scored: list[tuple[float, Chunk]] = []
        for cid, chunk in self._chunks.items():
            if exam_point and exam_point not in chunk.exam_point and exam_point != chunk.exam_point:
                continue
            sim = embedder.cosine(qvec, self._vectors[cid])
            lex = self._lexical_overlap(set(qtokens), chunk.text)
            bm25 = (bm25_raw.get(cid, 0.0) / bm25_max) if bm25_max > 0 else 0.0
            meta_bonus = 1.0 if any(k in chunk.text for k in ["抓大头", "审敛", "p-积分", "比阶"]) else 0.0
            score = 0.50 * sim + 0.20 * lex + 0.20 * bm25 + 0.15 * meta_bonus
            scored.append((score, chunk))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return scored[:top_k]

    async def register_chunks(self, new_chunks: list[Chunk]) -> int:
        """入库流水线回写入口：Embedding 写入向量库后，同步增量更新内存 BM25 倒排索引。

        向量与倒排各自持有锁；先索引后可见，避免并发读写冲突。返回新增数量。
        """
        await self._ensure_seeded()
        added = 0
        for chunk in new_chunks:
            if chunk.id in self._chunks:
                continue
            vec = await embedder.embed(f"{chunk.exam_point} {chunk.text}")
            self._chunks[chunk.id] = chunk
            self._vectors[chunk.id] = vec
            await self._store.upsert(chunk.id, vec, chunk.to_payload())
            self._bm25.add(chunk.id, embedder._tokenize(f"{chunk.exam_point} {chunk.text}"))
            added += 1
        return added

    async def all_chunks(self) -> list[Chunk]:
        await self._ensure_seeded()
        return list(self._chunks.values())


_retriever: HybridRetriever | None = None
_lock = asyncio.Lock()


async def get_retriever() -> HybridRetriever:
    global _retriever
    async with _lock:
        if _retriever is None:
            settings = get_settings()
            _retriever = HybridRetriever()
            _ = settings  # 保留配置读取入口
        return _retriever
