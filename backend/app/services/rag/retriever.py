# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""混合检索器：向量相似度 + BM25 倒排 + 租户/课程作用域隔离 + 时序衰减 + 邻近切片窗口。

**双路召回**（本轮核心修复）
---------------------------
Dense（向量）与 Sparse（BM25）各自独立召回 Top-N，并集进候选池后再统一加权。
此前只把查询向量交给 `store.search()`，BM25 仅作候选池**内部**的重排序信号——
这是"做了混合重排、却没做混合召回"的经典错误：向量检索漏掉的关键切片，
BM25 权重再高也永远捞不回来（它不在池子里，加权公式给它算不出分）。

**隔离边界**：租户（最外层，服务端权威） → 课程（次外层）。

优先写入/查询 Qdrant（带 tenant_id + course_id payload filter）；未配置 Qdrant
时自动使用进程内向量索引（零依赖兜底）。进程内 BM25 倒排索引随每次向量入库
同步增量更新，读写锁保证并发安全。
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import threading
import time
from datetime import date

from app.core.config import get_settings
from app.core.tenancy import DEFAULT_TENANT, multi_tenant_enabled
from app.db.vector_store import _tenant_filter_active, get_vector_store
from app.services.rag import catalog, corpus, embedder
from app.services.rag.chunker import Chunk

# 此前本模块引用了 `_logger` 却从未定义——两处"优雅降级"分支
# （store.search 失败 / 精排失败）一旦真的触发就会抛 NameError，
# 把降级变成崩溃。降级路径必须比主路径更可靠。
_logger = logging.getLogger("app.rag.retriever")


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
    """内存 BM25 关键词倒排索引：k1=1.5, b=0.75 经典参数。

    租户分区：postings 仍是全局一份（词项 → 文档的倒排关系必须全局共享，
    否则每个租户都要重建一遍词典），但每个 chunk 记录其租户归属，
    `scores()` 只返回**本租户**的得分。

    IDF 刻意保持**全库统计**而非租户内统计：租户语料小的时候，
    某个词在一两份文档里出现就会让 IDF 剧烈失真，"普遍出现的词"被算成
    "罕见词"从而被过度加权。隔离靠过滤返回结果实现，不靠改统计口径。
    """

    K1, B = 1.5, 0.75

    def __init__(self) -> None:
        self._lock = RWLock()
        self._postings: dict[str, dict[str, int]] = {}  # term -> {chunk_id: tf}
        self._doc_len: dict[str, int] = {}
        self._tenant_of: dict[str, str] = {}            # chunk_id -> 租户归属
        self._avg_len = 1.0

    def add(self, chunk_id: str, tokens: list[str], tenant_id: str = DEFAULT_TENANT) -> None:
        with self._lock:
            for tok in tokens:
                postings = self._postings.setdefault(tok, {})
                postings[chunk_id] = postings.get(chunk_id, 0) + 1
            self._doc_len[chunk_id] = len(tokens) or 1
            self._tenant_of[chunk_id] = tenant_id or DEFAULT_TENANT
            total = sum(self._doc_len.values())
            self._avg_len = total / max(len(self._doc_len), 1)

    def scores(
        self, query_tokens: list[str], tenant_id: str | None = None
    ) -> dict[str, float]:
        """对全量文档计算 BM25 得分（N 为千级，全扫足够）。

        `tenant_id` 非空且多租户模式开启时，只返回该租户的文档得分——
        跨租户文档在**打分阶段就不可见**，而不是"打完分再筛掉"。
        """
        restrict = _tenant_filter_active(tenant_id)
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
                    if restrict and self._tenant_of.get(cid, DEFAULT_TENANT) != tenant_id:
                        continue
                    dl = self._doc_len.get(cid, 1)
                    denom = tf + self.K1 * (1 - self.B + self.B * dl / self._avg_len)
                    scores[cid] = scores.get(cid, 0.0) + idf * (tf * (self.K1 + 1)) / denom
            return scores
        finally:
            self._lock.release_read()

    def tenant_of(self, chunk_id: str) -> str:
        self._lock.acquire_read()
        try:
            return self._tenant_of.get(chunk_id, DEFAULT_TENANT)
        finally:
            self._lock.release_read()

    def remove(self, chunk_ids: list[str]) -> int:
        """按 id 摘除倒排条目（入库失败回滚 / 跨副本重建索引前的清理）。

        必须把 `_doc_len`、`_tenant_of` 一起摘干净：只删 postings 会留下
        悬空的文档长度，`_avg_len` 随之偏大、BM25 归一化项失真，
        表现为"删了切片反而让别的切片打分变低"这种极难归因的偏差。
        """
        wanted = [str(c) for c in chunk_ids if c]
        if not wanted:
            return 0
        removed = 0
        with self._lock:
            for cid in wanted:
                if cid not in self._doc_len and cid not in self._tenant_of:
                    continue
                for term in list(self._postings):
                    postings = self._postings[term]
                    if postings.pop(cid, None) is not None and not postings:
                        self._postings.pop(term, None)
                self._doc_len.pop(cid, None)
                self._tenant_of.pop(cid, None)
                removed += 1
            total = sum(self._doc_len.values())
            self._avg_len = total / max(len(self._doc_len), 1)
        return removed


# 时间偏好默认值按场景走：窄范围复习（周测/单元）保留较高权重——近期内容
# 与考点高度重合；宽范围复习（月考/期末）跨度大，降到很低但非强制归零。
# 注意：时间权重只是"排序偏好"，防幻觉靠 canonical 定版 + 考点精准匹配。
_DEFAULT_ALPHA_BY_SCOPE = {
    "lecture": 0.30,
    "practice": 0.30,    # lecture 的语义别名（做题场景）
    "review_narrow": 0.20,  # 周测 / 单元复习
    "review_broad": 0.05,   # 月考 / 期末复习
    "review": 0.05,         # 兼容旧值：未声明范围的复习按宽处理
    "explore": 0.0,         # 拓展解法：纯语义
}

# ---------------------------------------------------------------- RRF ----
# 倒数排名融合的平滑常数（Cormack et al. 2009 的经典取值 60）。
# k 越大，头部名次的优势越平缓：k=60 时 rank1/rank10 = (70/61) ≈ 1.15，
# 即"第一名只比第十名强 15%"。这正是 RRF 的价值——不放大单路信号的分值幅度。
RRF_K = 60

# 语义调制锚点：RRF 只看名次，**任何**查询都会有一个"第一名"，哪怕全库
# 都与它无关。若不调制，一条与问题毫不相干的切片只要在某一路排靠前就能
# 拿到高分——实测 65 条候选中 41 条越过 0.42 阈值，绝对阈值判定彻底失效。
# 因此用向量语义分（绝对量）作调制因子，使融合分重新具备绝对相关性语义：
#   base = rrf_norm * (ANCHOR + (1-ANCHOR) * max(0, sim))
# sim=0 时保留 ANCHOR 比例的分数（不归零，因为 BM25 精确命中本身也是证据），
# sim=1 时满分保留。ANCHOR 越大越依赖名次，越小越依赖绝对值。
RRF_SEMANTIC_ANCHOR = 0.5


def _ranks(values: dict[str, float]) -> dict[str, int]:
    """把各候选的信号值转成降序名次（1 起）。

    只对**正向证据**排名：值为 0 或负的候选不参与。
    理由：RRF 丢弃分值幅度、只保留顺序，如果给"全体为零"的信号也分配
    1..N 的名次，就等于凭空捏造区分度——一个查询与所有切片都无关键词
    重合时，BM25 仍然会"排出名次"，那一组名次是纯噪声。
    同分按 id 排序，保证结果可复现。
    """
    positive = [(k, v) for k, v in values.items() if v > 0]
    positive.sort(key=lambda kv: (-kv[1], kv[0]))
    return {k: i + 1 for i, (k, _v) in enumerate(positive)}


class HybridRetriever:
    def __init__(self) -> None:
        self._store = get_vector_store()
        self._chunks: dict[str, Chunk] = {}
        self._vectors: dict[str, list[float]] = {}
        self._bm25 = BM25Index()
        self._seeded = False
        # 多副本索引同步（见 services/rag/catalog.py）：记录本进程已同步到的
        # 目录版本号 + 上次检查时间，把"比对版本"节流到每 N 秒一次。
        self._synced_versions: dict[str, int] = {}
        self._last_catalog_check: dict[str, float] = {}
        try:
            self._catalog_check_interval_s = float(
                os.getenv("RAG_CATALOG_SYNC_INTERVAL_S", "3") or 3
            )
        except ValueError:
            self._catalog_check_interval_s = 3.0

    async def _ensure_seeded(self) -> None:
        """首次检索前决定是否灌入内置演示语料（每个进程只做一次）。

        真实部署（配了真实模型 Key）默认不再灌入，避免演示数据混进生产知识库；
        需要时可用 SEED_DEMO_CORPUS=1 显式开启，或用 =0 在演示模式下也关闭。
        演示语料统一归入默认租户——它属于演示身份，不归属任何真实租户。
        """
        if self._seeded:
            return
        self._seeded = True
        if not get_settings().seed_demo_corpus_effective:
            return
        for item in corpus.SEED_CHUNKS:
            chunk = Chunk(**item)
            chunk.tenant_id = DEFAULT_TENANT
            self._chunks[chunk.id] = chunk
            vec, degraded = await embedder.embed_with_status(f"{chunk.exam_point} {chunk.text}")
            self._bm25.add(
                chunk.id,
                embedder.bm25_tokenize(f"{chunk.exam_point} {chunk.text}"),
                chunk.tenant_id,
            )
            if degraded:
                # 与 register_chunks 同一口径：降级期不把哈希向量写进主空间
                continue
            self._vectors[chunk.id] = vec
            await self._store.upsert(chunk.id, vec, chunk.to_payload())

    @staticmethod
    def _lexical_overlap(query_tokens: set[str], text: str) -> float:
        if not query_tokens:
            return 0.0
        # 与 BM25 共用 bigram 口径：两者都是"关键词通道"，
        # 用 `_tokenize` 会让中文整句变成一个 token，交集恒为空（信号恒 0）。
        hay = set(embedder.bm25_tokenize(text))
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
        time_alpha_override: float | None = None,
        canonical_bonus_override: float | None = None,
        chapter: str | None = None,
        tenant_id: str = DEFAULT_TENANT,
    ) -> list[Chunk]:
        scored = await self.retrieve_scored(
            query, top_k, exam_point, course_id, retrieval_mode,
            time_alpha_override, canonical_bonus_override, chapter,
            tenant_id=tenant_id,
        )
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
        time_alpha_override: float | None = None,
        canonical_bonus_override: float | None = None,
        chapter: str | None = None,
        tenant_id: str = DEFAULT_TENANT,
    ) -> list[tuple[float, Chunk]]:
        """返回 (融合得分, 切片)：0.50 向量 + 0.20 词面 + 0.20 BM25 + 0.15 元数据。

        - tenant_id 最外层强作用域：数据层硬过滤（Qdrant must / 内存子集收窄）
        - course_id 次外层强作用域：非空时只在该课程的切片内检索
        - retrieval_mode：lecture（随堂，时间衰减提权近讲）/ review（备考，纯语义跨月，
          canonical 仍生效）/ explore（拓展解法：解除 canonical 与章节聚合，纯语义）
        - 权重系数来自 runtime_config 的 retrieval 预设档位（strict/balanced/explore）
          + 管理后台高级覆盖，热生效
        """
        await self._ensure_seeded()
        # 多副本一致性：别的 Worker 入库/定版后，本副本的关键词索引会落后。
        # 这里做一次**节流后**的版本比对，落后才重载（见 catalog.py 的取舍说明）。
        await self._maybe_sync_catalog(tenant_id)
        from app.core.runtime_config import effective as cfg_effective

        weights = cfg_effective("retrieval")
        # 查询向量的**降级状态必须向下传递**：降级时拿到的是 256 维哈希向量，
        # 与集合里的真实向量不同空间。此时若继续走 dense 通道，cosine 会按短维度
        # 截断比较，稳定返回错误结果——宁可退化成纯稀疏检索（BM25 + 词面），
        # 也不要一个"看起来有分、实则无关"的排序。
        qvec, q_degraded = await embedder.embed_with_status(query)
        qtokens = embedder.bm25_tokenize(query)
        # 租户过滤必须在**召回**与**打分**两端同时生效，缺任一端都可能越权：
        # 只过滤召回，则 BM25 打分仍会看到跨租户文档的 tf/idf 统计；
        # 只过滤打分，则跨租户文档已进候选池，后续任何分支漏判即泄漏。
        scoped_tenant = tenant_id if _tenant_filter_active(tenant_id) else None
        bm25_raw = self._bm25.scores(qtokens, tenant_id=scoped_tenant)
        bm25_max = max(bm25_raw.values(), default=0.0)
        # 模式映射：canonical 权重与时间衰减分离——
        #   时间衰减回答"现在在讲什么"（仅随堂），canonical 回答"这道题该用哪个方法"
        explore = retrieval_mode == "explore"
        # α 三级来源：请求 override（最高）> scope 场景默认 > 后台调权预设
        if time_alpha_override is not None:
            alpha = max(0.0, min(1.0, float(time_alpha_override)))
        else:
            alpha = (
                0.0 if explore
                else _DEFAULT_ALPHA_BY_SCOPE.get(retrieval_mode, float(weights["time_alpha"]))
            )
        # canonical 定版权重：请求 override > 后台预设；explore 恒 0（拓展解法）
        canonical_w = (
            0.0 if explore
            else float(canonical_bonus_override) if canonical_bonus_override is not None
            else float(weights["canonical_bonus"])
        )
        lam = 0.05
        today = date.today()

        # 每路召回深度：默认过召回 4 倍给融合与精排留余量；
        # 部署档位的 coarse_top_k 是下限（eco=10 / standard=20 / performance=40）
        from app.core import profiles as _profiles

        _prof = _profiles.effective()
        recall_depth = max(top_k * 4, int(_prof["coarse_top_k"]), 1)

        # ---- Dense 路：向量近邻 ----
        # 候选池经 store.search（Qdrant 带 tenant_id + course_id payload filter /
        # 内存库等价余弦），多副本部署时结果由共享向量库决定，不依赖进程内字典。
        # 远端命中的切片若不在本进程缓存，用 payload 重建元数据。
        if q_degraded:
            # 降级：查询向量不可信 → 完全不进 dense 通道。融合里 sim 恒为 0，
            # 语义锚点按设计保留 ANCHOR 比例（"无语义证据"而非"语义无关"）。
            _logger.warning(
                "查询 embedding 处于降级（哈希空间），本次跳过向量通道，仅用 BM25 + 词面召回"
            )
            hits: list[tuple[str, float, dict]] = []
        else:
            try:
                hits = await self._store.search(
                    qvec, top_k=recall_depth, course_id=course_id or None,
                    tenant_id=tenant_id,
                )
            except Exception as exc:
                _logger.warning("store.search 失败，回退进程内候选池: %s", exc)
                # 进程内兜底只用 `_vectors` 里**同空间**的向量：降级期间的切片
                # 本就不写向量（见 register_chunks），所以这里不会混入哈希向量。
                hits = [
                    (cid, embedder.cosine(qvec, self._vectors[cid]), self._chunks[cid].to_payload())
                    for cid in self._vectors
                    if not course_id or self._chunks[cid].course_id == course_id
                ]
                if scoped_tenant:
                    hits = [
                        (pid, sim, pl) for pid, sim, pl in hits
                        if str((pl or {}).get("tenant_id") or DEFAULT_TENANT) == scoped_tenant
                    ]

        candidate_map: dict[str, tuple[float, Chunk]] = {}
        for pid, sim, payload in hits:
            chunk = self._chunks.get(pid)
            if chunk is None:
                payload = dict(payload or {})
                payload.setdefault("id", pid)
                try:
                    chunk = Chunk(**payload)
                except TypeError:
                    continue  # payload 缺关键字段（脏数据）跳过
                self._chunks[pid] = chunk
            candidate_map[pid] = (float(sim), chunk)

        # ---- Sparse 路：BM25 关键词召回（本轮核心修复）----
        # 此前 BM25 只作为"候选池内部的重排序信号"参与，从未贡献召回：
        # 加权公式里的 `bm25_raw.get(cid)` 只对**已在池中**的 cid 有值，
        # 于是向量检索漏掉的关键切片永远拿不到分，也永远进不了结果。
        # 结果就是 Dense 单独决定召回上限，长尾自然语言场景下 MRR 被锁死。
        dense_ids = set(candidate_map)
        sparse_added = 0
        for cid, _bm in sorted(bm25_raw.items(), key=lambda kv: kv[1], reverse=True)[:recall_depth]:
            if cid in dense_ids:
                continue
            chunk = self._chunks.get(cid)
            if chunk is None:
                continue  # 倒排索引指向的切片不在本进程缓存（脏索引），跳过
            # Sparse 路必须遵守与 Dense 路**完全相同**的作用域，
            # 否则它会成为绕过租户/课程隔离的后门。
            if course_id and chunk.course_id != course_id:
                continue
            if scoped_tenant and chunk.tenant_id != scoped_tenant:
                continue
            # 向量分补算：本进程有向量则算真余弦；没有则记 0，该切片
            # 仍可凭 BM25 与词面信号参与竞争，只是没有语义分加持。
            vec = self._vectors.get(cid)
            candidate_map[cid] = (float(embedder.cosine(qvec, vec)) if vec else 0.0, chunk)
            sparse_added += 1
        if sparse_added:
            _logger.debug(
                "双路召回：Dense %d 条 + Sparse 新增 %d 条 = %d 条候选",
                len(dense_ids), sparse_added, len(candidate_map),
            )

        # 版本去重：被显式 supersedes 的旧解法不参与竞争，避免 LLM"串戏"
        candidates = self._filter_superseded([c for _, c in candidate_map.values()])
        sim_of = {c.id: candidate_map[c.id][0] for c in candidates if c.id in candidate_map}

        # ---- 阶段一：作用域过滤 + 信号采集 ----
        # 必须先算全所有候选的信号，RRF 才能知道各路的完整名次。
        kept: list[Chunk] = []
        signals: dict[str, tuple[float, float, float]] = {}  # cid -> (sim, lex, bm25)
        for chunk in candidates:
            if exam_point and exam_point not in chunk.exam_point and exam_point != chunk.exam_point:
                continue
            # 章节软过滤：限定了 chapter 时，带章节元数据的切片需匹配才保留；
            # 无章节元数据的切片（演示种子/存量数据）放行，避免把整个库过滤成空
            if chapter and chunk.chapter and chapter not in chunk.chapter and chapter != chunk.chapter:
                continue
            cid = chunk.id
            lex = self._lexical_overlap(set(qtokens), chunk.text)
            bm25 = (bm25_raw.get(cid, 0.0) / bm25_max) if bm25_max > 0 else 0.0
            signals[cid] = (sim_of.get(cid, 0.0), lex, bm25)
            kept.append(chunk)

        # ---- 阶段二：融合打分 ----
        w_vec = float(weights["vector"])
        w_lex = float(weights["lexical"])
        w_bm25 = float(weights["bm25"])
        fusion = str(weights.get("fusion") or "weighted")

        base_of: dict[str, float] = {}
        if fusion == "rrf":
            # 三路各自排名，再按 1/(k+rank) 汇总。
            # 归一化分母用**理论最大值**（三路都排第一）而非实际最大值：
            # 用实际最大值会让任何查询的 top1 恒等于 1.0，"这条到底有多相关"
            # 的信息被彻底抹掉。
            rank_dense = _ranks({c.id: signals[c.id][0] for c in kept})
            rank_lex = _ranks({c.id: signals[c.id][1] for c in kept})
            rank_bm25 = _ranks({c.id: signals[c.id][2] for c in kept})
            denom = (w_vec + w_lex + w_bm25) / (RRF_K + 1)
            for c in kept:
                cid = c.id
                rrf = 0.0
                if cid in rank_dense:
                    rrf += w_vec / (RRF_K + rank_dense[cid])
                if cid in rank_lex:
                    rrf += w_lex / (RRF_K + rank_lex[cid])
                if cid in rank_bm25:
                    rrf += w_bm25 / (RRF_K + rank_bm25[cid])
                rrf_norm = (rrf / denom) if denom > 0 else 0.0
                # 语义调制：把"名次"重新锚回"绝对相关性"，否则 min_score
                # 与 GENERAL_RELEVANCE_FLOOR 在 RRF 下会失效（详见常量注释）
                sim = signals[cid][0]
                base_of[cid] = rrf_norm * (
                    RRF_SEMANTIC_ANCHOR
                    + (1.0 - RRF_SEMANTIC_ANCHOR) * max(0.0, sim)
                )
        else:
            # 加权和（fusion=weighted，可选）：分数是绝对量纲，
            # 与 min_score / canonical_bonus 直接配套，但两路分值尺度不可比
            # （长查询 BM25 偏高、短查询词面偏低），需要按场景配平权重。
            for c in kept:
                sim, lex, bm25 = signals[c.id]
                base_of[c.id] = w_vec * sim + w_lex * lex + w_bm25 * bm25

        scored: list[tuple[float, Chunk]] = []
        for chunk in kept:
            base = base_of[chunk.id]
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

        # 二阶段精排：粗排候选池交给 Cross-Encoder 重打分 + 门控权威加权。
        # 未配置模型时 RerankPipeline.active 为 False，此处零开销直接返回粗排结果。
        try:
            from app.services.rag.reranker import RerankConfig, RerankPipeline, get_reranker
            from app.core.runtime_config import effective as rc_effective

            rc = rc_effective("rerank")
            # 部署档位 rrf_only（eco 档默认）：强制跳过远程精排，
            # 抹去外部 HTTP 延迟与并发连接开销，RRF 粗排直通（实测 MRR 0.90）
            force_rrf_only = _prof.get("rerank_mode") == "rrf_only"
            pipeline = RerankPipeline(
                get_reranker(),
                RerankConfig(
                    enabled=bool(rc.get("enabled")) and not force_rrf_only,
                    tau=float(rc.get("tau", 0.42)),
                    beta=float(rc.get("beta", 0.30)),
                    recall_pool=int(rc.get("recall_pool", 18)),
                ),
            )
            if pipeline.active:
                # 精排需要更宽的候选池：先把粗排放宽到 recall_pool，再交给精排收敛
                scored = await pipeline.run(query, scored[: pipeline.recall_pool])
        except Exception as exc:
            _logger.warning("精排失败，沿用粗排结果: %s", exc)

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
        租户归属由调用方（ingest）在 `_tag_scope` 阶段服务端注入，此处只做落库。

        **降级期不写向量**：embedding 接口挂掉时拿到的是 256 维哈希向量，与集合里的
        真实向量不同空间——写进去就是永久污染（且无法区分污染点）。此时只把切片
        放进 BM25 稀疏索引与进程内缓存：检索仍能通过关键词通道命中它，
        语义通道等接口恢复后重建即可。
        """
        await self._ensure_seeded()
        added = 0
        degraded_skipped = 0
        for chunk in new_chunks:
            if chunk.id in self._chunks:
                continue
            if not (chunk.text or "").strip():
                continue  # 兜底：任何来源的空切片都不入向量库（占位 top_k 且拉低检索信噪比）
            vec, degraded = await embedder.embed_with_status(f"{chunk.exam_point} {chunk.text}")
            self._chunks[chunk.id] = chunk
            self._bm25.add(
                chunk.id,
                embedder.bm25_tokenize(f"{chunk.exam_point} {chunk.text}"),
                chunk.tenant_id,
            )
            if degraded:
                # 不进 `_vectors`、不进向量库：避免污染主空间
                degraded_skipped += 1
                added += 1
                continue
            self._vectors[chunk.id] = vec
            await self._store.upsert(chunk.id, vec, chunk.to_payload())
            added += 1
        if degraded_skipped:
            _logger.warning(
                "embedding 降级：%d 条切片仅进 BM25 稀疏索引、未写向量（接口恢复后需重建）",
                degraded_skipped,
            )
        if added:
            # 通知其他副本"目录变了"（版本号单调递增，落后者下一次查询自然发现）
            for tenant in {c.tenant_id for c in new_chunks}:
                catalog.bump(tenant)
        return added

    async def unregister_chunks(
        self, chunk_ids: list[str], *, tenants: set[str] | None = None
    ) -> int:
        """回滚：把刚写入的切片从**向量库与进程内索引**一并摘除。

        为什么需要它（孤儿数据）
        ------------------------
        入库是"先写向量、再写关系库资产记录"。若第二步失败（或进程在两步之间被强杀），
        向量库里就留下一条**没有任何业务记录指向它**的切片：检索会命中它、
        后台列表却看不到它，用户点进证据抽屉也找不到来源——比直接报错更难排查。
        补偿删除必须**同时**清掉向量库与内存索引（只清一边，本进程仍会继续返回幽灵结果）。

        `tenants` 由调用方给出（它知道这批切片属于谁）：删除在向量库里是
        **受租户条件约束**的，传错 id 也删不到别的租户的数据。
        """
        ids = [str(c) for c in chunk_ids if c]
        if not ids:
            return 0
        for cid in ids:
            self._chunks.pop(cid, None)
            self._vectors.pop(cid, None)
        removed_in_mem = self._bm25.remove(ids)

        scope_tenants = tenants or {DEFAULT_TENANT}
        for tenant in scope_tenants:
            try:
                await self._store.delete(ids, tenant_id=tenant)
            except Exception as exc:  # noqa: BLE001 - 补偿失败不能盖住原始异常
                _logger.warning("向量回滚删除失败（可能残留孤儿切片，需人工核对）: %s", exc)
            catalog.bump(tenant)
        _logger.info("回滚切片：内存 %d 条 / 向量库 %d 条", removed_in_mem, len(ids))
        return removed_in_mem

    async def _maybe_sync_catalog(self, tenant_id: str) -> None:
        """按需从共享向量库重建本进程的关键词索引（多副本一致性）。

        检查是**节流**的（默认每 3 秒最多一次版本比对），所以热路径上通常只是一次
        内存时间比较；只有版本真的变了才会发生一次重载。
        """
        if self._catalog_check_interval_s > 0:
            last = self._last_catalog_check.get(tenant_id, 0.0)
            if time.monotonic() - last < self._catalog_check_interval_s:
                return
        self._last_catalog_check[tenant_id] = time.monotonic()

        version = catalog.current_version(tenant_id)
        if version <= 0:
            return  # 无 Redis：单进程部署，本就没有跨进程同步问题
        if self._synced_versions.get(tenant_id) == version:
            return
        await self.sync_catalog(tenant_id, version)

    async def sync_catalog(self, tenant_id: str, version: int = 0) -> int:
        """从向量库拉取本租户切片元数据，补齐/刷新进程内索引。

        - 缺失的切片：加入 `_chunks` + BM25（关键词通道从此能看到它们）；
        - 已缓存的切片：只刷新**可变元数据**（定版标志/版本号/取代指针）——
          定版是写路径会改 payload 的操作，不刷新会让本副本继续按旧权威打分。
        不拉向量：BM25 用不到向量，而拉取向量会让重载成本成倍上升。
        """
        try:
            rows = await self._store.scroll_chunks(tenant_id=tenant_id)
        except Exception as exc:  # noqa: BLE001 - 同步失败不该让本次检索失败
            _logger.warning("目录同步失败（本次仍用已有索引）: %s", exc)
            return 0

        added = 0
        for pid, payload in rows:
            payload = dict(payload or {})
            existing = self._chunks.get(pid)
            if existing is not None:
                existing.is_canonical = bool(payload.get("is_canonical", existing.is_canonical))
                existing.method_version = int(payload.get("method_version", existing.method_version) or 1)
                existing.supersedes = payload.get("supersedes", existing.supersedes)
                continue
            payload.setdefault("id", pid)
            try:
                chunk = Chunk(**payload)
            except TypeError:
                continue  # 脏 payload 跳过（与检索路径同一策略）
            self._chunks[pid] = chunk
            self._bm25.add(
                pid,
                embedder.bm25_tokenize(f"{chunk.exam_point} {chunk.text}"),
                chunk.tenant_id,
            )
            added += 1

        if version > 0:
            self._synced_versions[tenant_id] = version
        if added:
            _logger.info("目录同步完成：新增 %d 条切片进入本副本关键词索引", added)
        return added

    async def all_chunks(self, tenant_id: str | None = None) -> list[Chunk]:
        """全量切片视图（packer 骨架 / 管理后台统计用）。

        `tenant_id` 非空且多租户模式开启时只返回该租户的切片——
        骨架用于邻近扩展，若跨租户返回，扩展出来的"邻居"会把别的
        机构内容拼进本题上下文（比检索泄漏更隐蔽）。
        """
        await self._ensure_seeded()
        # 邻近扩展与骨架都读 `_chunks`：本副本缺切片时上下文窗口会不完整，
        # 所以这里同样做一次同步检查（与 retrieve_scored 同一入口口径）。
        await self._maybe_sync_catalog(tenant_id or DEFAULT_TENANT)
        chunks = list(self._chunks.values())
        if _tenant_filter_active(tenant_id):
            chunks = [c for c in chunks if c.tenant_id == tenant_id]
        return chunks

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
                    # 租户必须同域：否则"设为定版"会把别的机构同考点的定版降级，
                    # 这是跨租户**写**越权——比读越权更严重（静默破坏他人数据）
                    and other.tenant_id == target.tenant_id
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
        # 定版会改 payload（is_canonical / method_version / supersedes），
        # 别的副本必须能发现——否则它们会继续按旧权威给分。
        catalog.bump(target.tenant_id)
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
