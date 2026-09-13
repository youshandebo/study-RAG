"""粗排 MRR 对比：bm25_tokenize（bigram） vs _tokenize（整句单 token）。

第 14 轮实测记录：粗排无法区分定版优劣，MRR 受限于召回质量。
本脚本用夹具量化第 16 轮改动的实际收益。
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("SEED_DEMO_CORPUS", "0")


async def run_case(use_old_tokenizer: bool):
    import app.services.rag.embedder as emb
    from app.db.vector_store import InMemoryVectorStore
    from app.services.rag.retriever import HybridRetriever
    from tests.fixtures import multichapter as fx

    saved = emb.bm25_tokenize
    if use_old_tokenizer:
        emb.bm25_tokenize = emb._tokenize  # type: ignore[assignment]
    try:
        r = HybridRetriever()
        await r._ensure_seeded()
        r._store = InMemoryVectorStore()  # 隔离向量库，避免两次运行互相污染
        await r.register_chunks(fx.FIXTURE_CHUNKS)

        rows = []
        rr = 0.0
        for q in fx.FIXTURE_QUERIES:
            hits = await r.retrieve_scored(
                q["query"], top_k=15, course_id=q["course_id"]
            )
            rels = set(q.get("expect_relevant_chunks") or [q["expect_relevant_chunk"]])
            rank = next(
                (i for i, (_s, c) in enumerate(hits, 1) if c.id in rels), None
            )
            rr += (1.0 / rank) if rank else 0.0
            rows.append((q["query_id"], rank, [c.id for _, c in hits[:3]]))
        return rr / len(fx.FIXTURE_QUERIES), rows
    finally:
        emb.bm25_tokenize = saved


async def main() -> None:
    print("=" * 72)
    for label, old in (("修复前（_tokenize：中文整句单 token）", True),
                       ("修复后（bm25_tokenize：中文 bigram）", False)):
        mrr, rows = await run_case(old)
        print(f"\n{label}")
        print(f"  粗排 MRR = {mrr:.4f}")
        for qid, rank, top3 in rows:
            print(f"  {qid}: 正解位次={rank}  top3={top3}")
    print("\n" + "=" * 72)

    # 分词效果直接对照
    from app.services.rag import embedder

    probe = "梯度下降的学习率衰减策略"
    query = "学习率衰减"
    old_q = set(embedder._tokenize(query))
    new_q = set(embedder.bm25_tokenize(query))
    old_d = set(embedder._tokenize(probe))
    new_d = set(embedder.bm25_tokenize(probe))
    print("分词对照")
    print(f"  查询 {query!r}")
    print(f"    _tokenize      -> {sorted(old_q)}")
    print(f"    bm25_tokenize  -> {sorted(new_q)}")
    print(f"  文档 {probe!r}")
    print(f"    _tokenize      -> {sorted(old_d)}")
    print(f"    bm25_tokenize  -> {sorted(new_d)}")
    print(f"  交集（词面/BM25 是否可能非零）")
    print(f"    _tokenize      -> {sorted(old_q & old_d)}  <-- 恒空 = 关键词通道失效")
    print(f"    bm25_tokenize  -> {sorted(new_q & new_d)}")


asyncio.run(main())
