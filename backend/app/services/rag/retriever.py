# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""混合检索器：向量相似度 + BM25 倒排 + 课程作用域隔离 + 时序衰减 + 邻近切片窗口。

优先写入/查询 Qdrant（带 course_id payload filter）；未配置 Qdrant 时自动使用
进程内向量索引（零依赖兜底）。进程内 BM25 倒排索引随每次向量入库同步增量
更新，读写锁保证并发安全。
"""
from __future__ import annotations

import asyncio
import math
import threading
from datetime import date

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
        course_id: str | None = None,
        retrieval_mode: str = "lecture",
        with_context_window: bool = True,
    ) -> list[Chunk]:
        scored = await self.retrieve_scored(query, top_k, exam_point, course_id, retrieval_mode)
        picked = (
            [c for _, c in scored]
            if min_score is None
            else [c for s, c in scored if s >= min_score][:top_k]
        )
        # 微观授课时序窗口：命中的切片自动前/后各拼接 1 个相邻时间戳切片，
        # 保证推导前提与结论完整进入 Prompt（course_id 相同才拼接）
        if with_context_window:
            picked = self._expand_with_neighbors(picked, course_id)
        return picked

    def _expand_with_neighbors(self, picked: list[Chunk], course_id: str | None) -> list[Chunk]:
        """In-lecture Context Window：按 (course_id, audio_id) 分组内的时间戳近邻拼接。"""
        out: list[Chunk] = []
        seen: set[str] = set()
        for chunk in picked:
            for neighbor in self._neighbors_of(chunk, course_id):
                if neighbor.id not in seen:
                    seen.add(neighbor.id)
                    out.append(neighbor)
            if chunk.id not in seen:
                seen.add(chunk.id)
                out.append(chunk)
        return out

    def _neighbors_of(self, chunk: Chunk, course_id: str | None) -> list[Chunk]:
        """同一堂课（audio_id）内按时间戳排序，取命中切片前后各 1 个。"""
        if course_id and chunk.course_id != course_id:
            return []
        siblings = sorted(
            (c for c in self._chunks.values() if c.audio_id == chunk.audio_id),
            key=lambda c: self._clock_ms(c.start),
        )
        idx = next((i for i, c in enumerate(siblings) if c.id == chunk.id), -1)
        if idx < 0:
            return []
        return [siblings[i] for i in (idx - 1, idx + 1) if 0 <= i < len(siblings)]

    @staticmethod
    def _clock_ms(clock: str) -> int:
        try:
            mm, ss = clock.split(":")
            return (int(mm) * 60 + int(ss)) * 1000
        except ValueError:
            return 0

    async def retrieve_scored(
        self,
        query: str,
        top_k: int = 3,
        exam_point: str | None = None,
        course_id: str | None = None,
        retrieval_mode: str = "lecture",
    ) -> list[tuple[float, Chunk]]:
        """返回 (融合得分, 切片)：0.50 向量 + 0.20 词面 + 0.20 BM25 + 0.15 元数据。

        - course_id 强作用域：非空时只在该课程的切片内检索（Qdrant 走 payload filter）
        - retrieval_mode：lecture（随堂，时间衰减提权近讲）/ review（备考，纯语义跨月，
          canonical 仍生效）/ explore（拓展解法：解除 canonical 与章节聚合，纯语义）
        - 权重系数来自 runtime_config 的 retrieval 预设档位（strict/balanced/explore）
          + 管理后台高级覆盖，热生效
        """
        await self._ensure_seeded()
        from app.core.runtime_config import effective as cfg_effective

        weights = cfg_effective("retrieval")
        qvec = await embedder.embed(query)
        qtokens = embedder._tokenize(query)
        bm25_raw = self._bm25.scores(qtokens)
        bm25_max = max(bm25_raw.values(), default=0.0)

        # 模式映射：canonical 权重与时间衰减分离——
        #   时间衰减回答"现在在讲什么"（仅随堂），canonical 回答"这道题该用哪个方法"
        explore = retrieval_mode == "explore"
        alpha = 0.0 if retrieval_mode in ("review", "explore") else float(weights["time_alpha"])
        canonical_w = 0.0 if explore else float(weights["canonical_bonus"])
        lam = 0.05
        today = date.today()

        # 候选池 + 版本去重：被显式 supersedes 的旧解法不参与竞争，避免 LLM"串戏"
        candidates = [
            c for c in self._chunks.values()
            if not course_id or c.course_id == course_id
        ]
        candidates = self._filter_superseded(candidates)

        scored: list[tuple[float, Chunk]] = []
        for chunk in candidates:
            if exam_point and exam_point not in chunk.exam_point and exam_point != chunk.exam_point:
                continue
            cid = chunk.id
            sim = embedder.cosine(qvec, self._vectors[cid])
            lex = self._lexical_overlap(set(qtokens), chunk.text)
            bm25 = (bm25_raw.get(cid, 0.0) / bm25_max) if bm25_max > 0 else 0.0
            meta_bonus = 1.0 if any(k in chunk.text for k in ["抓大头", "审敛", "p-积分", "比阶"]) else 0.0
            base = (
                float(weights["vector"]) * sim
                + float(weights["lexical"]) * lex
                + float(weights["bm25"]) * bm25
                + 0.15 * meta_bonus
            )
            # canonical 定版权威加分：explore 模式下为 0（学生已掌握老师方法，看别的思路）
            if chunk.is_canonical:
                base += canonical_w

            # 时间衰减：仅作用于非 canonical 切片（定版不随时间贬值，直到被新版本显式取代）
            if alpha > 0 and chunk.lecture_date and not chunk.is_canonical:
                try:
                    dt_days = max(0.0, (today - date.fromisoformat(chunk.lecture_date)).days)
                    base *= 1 + alpha * math.exp(-lam * dt_days)
                except ValueError:
                    pass
            scored.append((base, chunk))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return scored[:top_k]

    @staticmethod
    def _filter_superseded(candidates: list[Chunk]) -> list[Chunk]:
        """考点内版本去重：supersedes 链上被取代的旧版本直接剔除候选池。"""
        superseded_ids = {c.supersedes for c in candidates if c.supersedes}
        if not superseded_ids:
            return candidates
        return [c for c in candidates if c.id not in superseded_ids]

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

    async def set_canonical(self, chunk_id: str, canonical: bool) -> Chunk | None:
        """老师定版操作：将切片设为/取消考点标准解法。

        设为定版时自动：同 exam_point（同课程）旧 canonical 降级并写入
        supersedes 指向新版本，method_version 在其基础上递增——版本关系显式
        声明，不靠衰减曲线隐式猜。Qdrant payload 同步更新。
        """
        await self._ensure_seeded()
        target = self._chunks.get(chunk_id)
        if target is None:
            return None
        target.is_canonical = canonical
        if canonical:
            deposed: Chunk | None = None
            for other in self._chunks.values():
                if (
                    other.id != chunk_id
                    and other.is_canonical
                    and other.exam_point == target.exam_point
                    and other.course_id == target.course_id
                ):
                    other.is_canonical = False
                    deposed = other
            if deposed is not None:
                # 版本链：新版本指向被取代的旧版本（_filter_superseded 据此剔除旧版）
                target.supersedes = deposed.id
                target.method_version = deposed.method_version + 1
        vec = self._vectors.get(chunk_id)
        if vec is not None:
            await self._store.upsert(chunk_id, vec, target.to_payload())
        return target


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
