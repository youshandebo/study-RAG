"""混合检索器：向量相似度 + 词面重合 + 考点元数据过滤 + 简易 Rerank。

优先写入/查询 Qdrant；未配置 Qdrant 时自动使用进程内向量索引（零依赖兜底），
保证检索链路在任何部署形态下均可运行。
"""
from __future__ import annotations

import asyncio

from app.core.config import get_settings
from app.db.vector_store import get_vector_store
from app.services.rag import corpus, embedder
from app.services.rag.chunker import Chunk


class HybridRetriever:
    def __init__(self) -> None:
        self._store = get_vector_store()
        self._chunks: dict[str, Chunk] = {}
        self._vectors: dict[str, list[float]] = {}
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
        """返回 (融合得分, 切片)，供调用方按相关性阈值过滤。"""
        await self._ensure_seeded()
        qvec = await embedder.embed(query)
        qtokens = set(embedder._tokenize(query))

        scored: list[tuple[float, Chunk]] = []
        for cid, chunk in self._chunks.items():
            if exam_point and exam_point not in chunk.exam_point and exam_point != chunk.exam_point:
                continue
            sim = embedder.cosine(qvec, self._vectors[cid])
            lex = self._lexical_overlap(qtokens, chunk.text)
            meta_bonus = 0.15 if any(k in chunk.text for k in ["抓大头", "审敛", "p-积分", "比阶"]) else 0.0
            scored.append((0.62 * sim + 0.28 * lex + meta_bonus, chunk))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return scored[:top_k]

    async def register_chunks(self, new_chunks: list[Chunk]) -> int:
        """入库流水线回写入口：向量化 + 入库 + 更新进程内索引。返回新增数量。"""
        await self._ensure_seeded()
        added = 0
        for chunk in new_chunks:
            if chunk.id in self._chunks:
                continue
            vec = await embedder.embed(f"{chunk.exam_point} {chunk.text}")
            self._chunks[chunk.id] = chunk
            self._vectors[chunk.id] = vec
            await self._store.upsert(chunk.id, vec, chunk.to_payload())
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
