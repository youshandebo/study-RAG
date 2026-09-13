#!/usr/bin/env python3
# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""粗排召回质量度量：在微型多章节夹具上计算 MRR，并对比融合策略。

用途
----
检索参数（权重、融合方式、分词口径）一改，效果好不好不能靠感觉。
本脚本给出可复现的数字：每条查询的正解位次、MRR、候选分数分布。

为什么分数分布也要打印
----------------------
`min_score` / `GENERAL_RELEVANCE_FLOOR` 这类**绝对阈值**只有在分数是
绝对量纲时才成立。RRF 的分数来自"名次的倒数"，量纲完全不同——
只看 MRR 会漏掉"排序变好了，但阈值再也拦不住东西"这类回归。

用法
----
    python scripts/measure_recall_mrr.py                 # 对比 weighted / rrf
    python scripts/measure_recall_mrr.py --fusion rrf    # 只看某一种
"""
from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("SEED_DEMO_CORPUS", "0")


async def run_case(fusion: str, use_legacy_tokenizer: bool = False):
    import app.services.rag.embedder as emb
    from app.core import runtime_config as rc
    from app.db.vector_store import InMemoryVectorStore
    from app.services.rag.retriever import HybridRetriever
    from tests.fixtures import multichapter as fx

    saved_tok = emb.bm25_tokenize
    if use_legacy_tokenizer:
        emb.bm25_tokenize = emb._tokenize  # type: ignore[assignment]

    rc.save_runtime_config({"retrieval": {"fusion": fusion}})
    try:
        r = HybridRetriever()
        await r._ensure_seeded()
        r._store = InMemoryVectorStore()  # 隔离向量库，避免多次运行互相污染
        await r.register_chunks(fx.FIXTURE_CHUNKS)

        rows = []
        rr = 0.0
        all_scores: list[float] = []
        for q in fx.FIXTURE_QUERIES:
            hits = await r.retrieve_scored(
                q["query"], top_k=15, course_id=q["course_id"]
            )
            rels = set(q.get("expect_relevant_chunks") or [q["expect_relevant_chunk"]])
            rank = next(
                (i for i, (_s, c) in enumerate(hits, 1) if c.id in rels), None
            )
            rr += (1.0 / rank) if rank else 0.0
            all_scores.extend(s for s, _c in hits)
            rel_score = next((s for s, c in hits if c.id in rels), None)
            rows.append((q["query_id"], rank, rel_score, [c.id for _, c in hits[:3]]))
        return rr / len(fx.FIXTURE_QUERIES), rows, all_scores
    finally:
        emb.bm25_tokenize = saved_tok
        rc.save_runtime_config({"retrieval": {"fusion": ""}})


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))
    return s[idx]


async def main() -> None:
    ap = argparse.ArgumentParser(description="粗排召回质量度量")
    ap.add_argument("--fusion", default="", choices=["", "weighted", "rrf"],
                    help="只测某一种融合；缺省对比 weighted / rrf")
    args = ap.parse_args()

    cases: list[tuple[str, str, bool]] = []
    if args.fusion:
        cases.append((args.fusion, args.fusion, False))
    else:
        cases.append(("weighted（默认）", "weighted", False))
        cases.append(("rrf", "rrf", False))
        cases.append(("weighted + 旧分词（对照）", "weighted", True))

    print("=" * 78)
    for label, fusion, legacy in cases:
        mrr, rows, scores = await run_case(fusion, legacy)
        print(f"\n【{label}】  粗排 MRR = {mrr:.4f}")
        for qid, rank, rel_score, top3 in rows:
            rs = f"{rel_score:.4f}" if rel_score is not None else "  n/a "
            print(f"  {qid}: 正解位次={rank}  正解分={rs}  top3={top3}")
        print(
            f"  分数分布: min={min(scores):.4f} p50={_pct(scores, 0.5):.4f} "
            f"p90={_pct(scores, 0.9):.4f} max={max(scores):.4f}"
        )
        above = sum(1 for s in scores if s >= 0.42)
        print(f"  阈值 0.42 以上候选: {above}/{len(scores)}")
    print("\n" + "=" * 78)
    print("提示：若 rrf 的分数分布与 weighted 差异大，说明依赖绝对量纲的")
    print("      min_score / GENERAL_RELEVANCE_FLOOR 需要随融合模式另行标定。")


asyncio.run(main())
