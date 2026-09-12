#!/usr/bin/env python3
# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""真实 Cross-Encoder Logit 抽取管线：给离线标定脚本喂**真实分布**的数据。

为什么需要它
------------
`scripts/eval_rerank_params.py` 用的是内置合成黄金集，它的 logit 是人为捏的。
合成数据只能证明"网格搜索 + 门控公式 + 平价测试"这套**机制自洽**，
无法给 (tau, beta) 提供物理依据——真实 Cross-Encoder 的 logit 尺度、
领域内负样本的长尾抬升，都不是合成值能替代的。

本脚本做的是"零人工低成本"取证：
    真实查询池 → 真实粗排召回 → 真实 ONNX 推理出原始 logit → 弱标注 → 喂入标定脚本

职责边界（重要）
----------------
本脚本**只导出数据，不做参数搜索**。搜索必须交给已通过平价测试的
`eval_rerank_params.py`，否则就会出现"两套评分语义"，门控行为与生产漂移。
导出格式与 `eval_rerank_params.load_dataset` 严格对齐。

弱标注（Silver Labeling）三条规则
---------------------------------
1. `is_canonical`   —— 直接取切片元数据，无需人工。
2. `is_relevant`    —— 不做逐条人工判读，采用**多路号票**（见 `judge_relevance`）：
       a) 考点命中：切片 exam_point 与查询携带的考点一致
       b) 章节命中：查询显式给出章节且切片同章
       c) 教师定版：切片是 canonical（教师亲自定过版 = 该考点权威答案）
   任一路命中即判相关。这是"弱"标签——它会有噪声，但**噪声是可度量的**：
   脚本会输出标注统计与对角线自检，让你知道这份标签有多可靠再决定是否采信。
3. 硬对抗负样本 —— 带 canonical 属性、但既非本考点也非本查询所属章节的切片。
   这类样本是标定的关键：它们与查询共享领域词汇，真实 logit 会被抬到
   0.4~0.6 的模糊带，正是 tau 需要拦截的对象。

用法
----
    # 1) 真实 ONNX 模型（推荐）
    python scripts/extract_rerank_dataset.py \\
        --queries data/queries.jsonl \\
        --model-path models/bge-reranker-base.onnx \\
        --tokenizer-dir models/bge-reranker-base \\
        --out data/eval_golden_real.json

    # 2) 无模型时的自检（logit 由确定性哈希生成，仅用于验证管线连通性，
    #    **产出的数据不可用于标定**，会被显式标记）
    python scripts/extract_rerank_dataset.py --queries data/queries.jsonl \\
        --out data/eval_golden_smoke.json --allow-stub

查询池 JSONL 每行一条：
    {"query": "抓大头的时候能不能把 ln x 丢掉？", "course_id": "math-calculus-101",
     "exam_point": "p-反常积分比较审敛法", "chapter": "5.3 反常积分"}

约束自检（`--report` 输出）
---------------------------
脚本会打出真实分布的两个物理特征，用来判断"这份数据到底是不是真实模型产出的"：
    * logit 带宽  —— 真实 Cross-Encoder 在领域内冲突样本上通常压缩在窄带内
    * 模糊带占比  —— sigmoid 落在 [0.40, 0.60] 的候选比例（真实数据里这个比例不低）
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import pathlib
import sys
from dataclasses import dataclass, field

# 复用生产检索器与精排器，保证候选池与打分口径和线上一致。
# 放在模块级（而非仅在 OnnxBackend 内）——stub 路径同样需要 import app.services.rag。
_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

# ------------------------------------------------------------------ 常量 --

# 模糊带：真实 Cross-Encoder 处理领域内冲突样本时极易踩进来的区间。
# 合成数据几乎不会落在这里（分数断层明显），因此它是"分布真伪"的判据之一。
FUZZY_LO, FUZZY_HI = 0.40, 0.60


def stable_sigmoid(z: float) -> float:
    """与 eval_rerank_params / reranker.py 同式的数值稳定 Sigmoid。"""
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    exp_z = math.exp(z)
    return exp_z / (1.0 + exp_z)


# ------------------------------------------------------------ 查询与召回 --


@dataclass
class QuerySpec:
    query: str
    course_id: str = ""
    exam_point: str = ""
    chapter: str = ""
    query_id: str = ""


@dataclass
class Recalled:
    chunk_id: str
    text: str
    exam_point: str
    chapter: str
    course_id: str
    is_canonical: bool
    course_match: bool  # 是否落在查询指定的 course_id 作用域内


@dataclass
class ExtractStats:
    queries: int = 0
    candidates: int = 0
    relevant: int = 0
    relevant_canonical: int = 0
    adversarial: int = 0
    logits: list[float] = field(default_factory=list)
    stub_used: bool = False

    def add_logits(self, values: list[float]) -> None:
        self.logits.extend(values)


# ---------------------------------------------------------------- 弱标注 --


def judge_relevance(q: QuerySpec, chunk: Recalled) -> tuple[bool, str]:
    """多路号票弱标注。返回 (是否相关, 命中理由)——理由留痕，便于人工抽检。"""
    if q.exam_point and chunk.exam_point and q.exam_point == chunk.exam_point:
        return True, "exam_point"
    if q.chapter and chunk.chapter and q.chapter == chunk.chapter:
        return True, "chapter"
    if chunk.is_canonical and chunk.course_match:
        return True, "canonical"
    return False, ""


def is_adversarial(chunk: Recalled, relevant: bool) -> bool:
    """硬对抗负样本：带定版权威属性，却与本次查询不相关。"""
    return chunk.is_canonical and not relevant


# ---------------------------------------------------------------- 推理器 --


class LogitBackend:
    """logit 推理后端抽象。真实实现走 ONNX，stub 实现仅供管线连通性自检。"""

    name = "base"

    def score_batch(self, query: str, texts: list[str]) -> list[float]:
        raise NotImplementedError

    def close(self) -> None:
        return None


class OnnxBackend(LogitBackend):
    """生产同源：复用 backend 的 OnnxReranker，保证打分口径与线上完全一致。"""

    name = "onnx"

    def __init__(self, model_path: str, tokenizer_dir: str = "") -> None:
        from app.services.rag.reranker import OnnxReranker

        self._impl = OnnxReranker(model_path, tokenizer_dir)

    def score_batch(self, query: str, texts: list[str]) -> list[float]:
        # 直接调 _logit，拿**未经 sigmoid 的原始值**——标定需要的是物理 logit，
        # sigmoid 留给离线脚本按同一公式复算，避免两处各转一次导致口径漂移。
        return [self._impl._logit(query, t) for t in texts]


class StubBackend(LogitBackend):
    """确定性桩：logit 由 query+text 哈希派生。

    **仅用于验证抽取管线的连通性**（读查询池 → 召回 → 落盘 → 喂标定脚本）。
    它产出的数据不具备任何物理意义，脚本会在输出里写 warning 字段，
    标定脚本读到该字段应拒绝采信。
    """

    name = "stub"

    def score_batch(self, query: str, texts: list[str]) -> list[float]:
        out: list[float] = []
        for t in texts:
            h = hashlib.sha256(f"{query}\x00{t}".encode("utf-8")).digest()
            # 映射到 [-2.0, 1.5]：真实 Cross-Encoder 的典型窄带
            raw = int.from_bytes(h[:4], "big") / 0xFFFFFFFF
            out.append(round(-2.0 + raw * 3.5, 4))
        return out


# ------------------------------------------------------------------ 主流程 --


def load_queries(path: str) -> list[QuerySpec]:
    p = pathlib.Path(path)
    if not p.exists():
        print(f"[错误] 查询池不存在: {p}", file=sys.stderr)
        raise SystemExit(2)
    specs: list[QuerySpec] = []
    for lineno, line in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"[错误] 查询池第 {lineno} 行不是合法 JSON: {exc}", file=sys.stderr)
            raise SystemExit(2)
        q = str(obj.get("query") or "").strip()
        if not q:
            continue
        specs.append(
            QuerySpec(
                query=q,
                course_id=str(obj.get("course_id") or ""),
                exam_point=str(obj.get("exam_point") or ""),
                chapter=str(obj.get("chapter") or ""),
                query_id=str(obj.get("query_id") or f"q{len(specs) + 1:03d}"),
            )
        )
    return specs


async def recall_for_query(
    q: QuerySpec, recall_depth: int, retriever=None
) -> list[Recalled]:
    """真实粗排召回。直接复用生产 HybridRetriever，保证候选池口径一致。

    注意用 `with_context_window=False`：抽取阶段要的是**离散候选**，
    带邻近窗口会把整段连续切片灌进来，污染候选集合与标注。

    retriever 显式传入时用于夹具模式（测试夹具注入的实例），否则取全局单例。
    """
    if retriever is None:
        from app.services.rag.retriever import get_retriever

        retriever = await get_retriever()
    course_id = q.course_id or None
    scored = await retriever.retrieve_scored(
        q.query,
        top_k=recall_depth,
        course_id=course_id,
        exam_point=q.exam_point or None,
        chapter=q.chapter or None,
    )
    out: list[Recalled] = []
    for _score, chunk in scored[:recall_depth]:
        out.append(
            Recalled(
                chunk_id=chunk.id,
                text=f"{chunk.exam_point} {chunk.text}".strip(),
                exam_point=chunk.exam_point,
                chapter=chunk.chapter,
                course_id=chunk.course_id,
                is_canonical=chunk.is_canonical,
                course_match=(not q.course_id) or chunk.course_id == q.course_id,
            )
        )
    return out


async def build_fixture_retriever():
    """构造注入了微型多章节夹具的检索器（不依赖真实视频库）。

    夹具位于 backend/tests/fixtures，是测试资产，不进生产种子语料。
    这里显式关闭演示种子，保证候选池里只有夹具切片，标定数据干净。
    """
    import os

    os.environ.setdefault("SEED_DEMO_CORPUS", "0")
    from app.services.rag.retriever import HybridRetriever
    from tests.fixtures import multichapter as fx

    retriever = HybridRetriever()
    await retriever.register_chunks(fx.FIXTURE_CHUNKS)
    return retriever, fx


def build_golden(
    specs: list[QuerySpec],
    backend: LogitBackend,
    recall_depth: int,
    stats: ExtractStats,
    retriever=None,
    ground_truth: dict[str, str] | None = None,
) -> list[dict]:
    """逐查询召回 → 打分 → 弱标注，产出与标定脚本对齐的黄金集结构。

    ground_truth: {query_id: relevant_chunk_id}。夹具自带精确真值，
    有此映射时优先采用它而非多路号票的弱标签——夹具的正确答案是刻意设计的
    （跨章节错配），弱标注的 chapter 通道在夹具场景下不适用（查询不带 chapter）。
    """
    dataset: list[dict] = []
    for q in specs:
        recalled = asyncio.run(recall_for_query(q, recall_depth, retriever))
        if not recalled:
            print(f"[跳过] {q.query_id} 召回为空：{q.query[:30]}")
            continue

        logits = backend.score_batch(q.query, [r.text for r in recalled])
        stats.add_logits(logits)
        stats.queries += 1

        gt_id = (ground_truth or {}).get(q.query_id)
        cands: list[dict] = []
        for r, z in zip(recalled, logits):
            if gt_id:
                # 夹具模式：真值直接给定，弱标注不参与
                relevant = r.chunk_id == gt_id
                why = "ground_truth" if relevant else "ground_truth_negative"
            else:
                relevant, why = judge_relevance(q, r)
            if relevant:
                stats.relevant += 1
                if r.is_canonical:
                    stats.relevant_canonical += 1
            if is_adversarial(r, relevant):
                stats.adversarial += 1
            cands.append(
                {
                    "chunk_id": r.chunk_id,
                    "logit": round(float(z), 6),
                    "is_canonical": bool(r.is_canonical),
                    "is_relevant": bool(relevant),
                    "label_reason": why,       # 留痕：人工抽检时可直接定位标注依据
                }
            )
        stats.candidates += len(cands)
        dataset.append(
            {
                "query_id": q.query_id,
                "query": q.query,
                # 溯源标记随每条 query 落盘：黄金集文件常常只被当作一个
                # 「候选数组」传递，头部信息容易在搬运中丢失。写进每一条里，
                # 标定脚本才能在任何情况下识别出数据来源。
                #
                # 标记从 backend 自身推导（而非读 stats.stub_used）——
                # stub_used 是 main() 设的外部状态，直接调 build_golden 时
                # 它还是 False，标记会与后端实际身份脱钩，闸门形同虚设。
                "provenance": backend.name,
                "stub": backend.name == "stub",
                "candidates": cands,
            }
        )
    return dataset


def distribution_report(stats: ExtractStats) -> dict:
    """真实分布自检：把"物理模型支撑"从形容词变成可打印的数字。"""
    if not stats.logits:
        return {}
    logits = sorted(stats.logits)
    n = len(logits)
    sig = [stable_sigmoid(z) for z in logits]
    fuzzy = sum(1 for s in sig if FUZZY_LO <= s <= FUZZY_HI)
    return {
        "logit_min": round(logits[0], 4),
        "logit_max": round(logits[-1], 4),
        "logit_band": round(logits[-1] - logits[0], 4),
        "logit_median": round(logits[n // 2], 4),
        "sig_range": [round(min(sig), 4), round(max(sig), 4)],
        "fuzzy_ratio": round(fuzzy / n, 4),
        "fuzzy_band": [FUZZY_LO, FUZZY_HI],
    }


def print_report(stats: ExtractStats, depth: int) -> None:
    rep = distribution_report(stats)
    print("=" * 62)
    print("抽取统计")
    print("-" * 62)
    print(f"  query 数            : {stats.queries}")
    print(f"  候选总数            : {stats.candidates}（召回深度 {depth}）")
    print(f"  判为相关            : {stats.relevant}")
    print(f"  相关且为定版        : {stats.relevant_canonical}")
    print(f"  硬对抗负样本        : {stats.adversarial}")
    if rep:
        print("-" * 62)
        print("真实分布自检（判断这份数据是否具备物理意义）")
        print(f"  logit 值域          : [{rep['logit_min']}, {rep['logit_max']}]")
        print(f"  logit 带宽          : {rep['logit_band']}")
        print(f"  sigmoid 值域        : {rep['sig_range']}")
        print(f"  模糊带 [0.40,0.60]  : {rep['fuzzy_ratio'] * 100:.1f}% 的候选落在其中")
    print("=" * 62)
    if stats.stub_used:
        print("!! 本次使用 stub 后端，logit 无物理意义，**禁止用于参数标定**")
        print("=" * 62)
    elif stats.relevant_canonical == 0:
        print("!! 未抽到【相关且为定版】的样本 → activation_rate 恒 0，")
        print("   标定脚本无法判断 beta 是否有效。请检查查询池是否覆盖了已定版考点。")
        print("=" * 62)
    elif stats.adversarial == 0:
        print("!! 未抽到硬对抗负样本 → false_boosts 恒 0，tau 网格会退化成常量。")
        print("   请确保查询池包含了与已定版考点同章节、但考点不同的提问。")
        print("=" * 62)

    # 语料规模自检：只有 1 个 canonical 或全部同章节时，弱标注会退化为
    # 「全部相关」，硬对抗样本根本无法产生（chapter 通道把同章切片全判相关）。
    if stats.candidates and stats.relevant == stats.candidates and not stats.stub_used:
        print("!! 全部候选都被判为相关 → 弱标注在此语料上失效。")
        print("   原因通常是：候选集里没有「同章节不同考点」的切片，chapter")
        print("   通道把所有同章切片都票成了正样本。需要先扩充语料（至少覆盖")
        print("   多个章节/考点），再跑真实标定。")
        print("=" * 62)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="真实 Cross-Encoder Logit 抽取管线")
    ap.add_argument("--queries", default="", help="查询池 JSONL 路径")
    ap.add_argument("--out", required=True, help="输出黄金集 JSON 路径")
    ap.add_argument("--model-path", default="", help="ONNX 模型路径（缺省走 stub 自检）")
    ap.add_argument("--tokenizer-dir", default="", help="tokenizer 目录")
    ap.add_argument("--recall-depth", type=int, default=15, help="每查询召回候选数，默认 15")
    ap.add_argument("--allow-stub", action="store_true", help="允许无模型时用 stub 后端跑通管线")
    ap.add_argument("--fixture", action="store_true",
                    help="用微型多章节夹具作为语料（免真实视频库，真值由夹具给定）")
    ap.add_argument("--report", action="store_true", help="只打印分布自检，不写文件")
    args = ap.parse_args(argv)

    # 夹具模式可省略 --queries（直接用夹具内置查询）；两者都不给则报错
    if not args.queries and not args.fixture:
        print("[错误] 需提供 --queries，或使用 --fixture 走内置夹具", file=sys.stderr)
        return 2

    specs = load_queries(args.queries) if args.queries else []
    if not specs and not args.fixture:
        print("[错误] 查询池为空", file=sys.stderr)
        return 2

    stats = ExtractStats()
    if args.model_path:
        backend: LogitBackend = OnnxBackend(args.model_path, args.tokenizer_dir)
    elif args.allow_stub:
        backend = StubBackend()
        print("[警告] 未提供 --model-path，使用 stub 后端。产出的 logit 无物理意义。", file=sys.stderr)
    else:
        print(
            "[错误] 未提供 --model-path。真实标定必须有真实模型；\n"
            "       若只想验证管线连通性，请显式加 --allow-stub（产出数据不可用于标定）。",
            file=sys.stderr,
        )
        return 2

    # 单一事实源：后端身份决定 stub 标记，不从命令行参数二次推导
    stats.stub_used = backend.name == "stub"

    # 夹具模式：候选池来自 backend/tests/fixtures 的微型多章节语料，
    # 不依赖真实视频库；真值由夹具直接给定（弱标注在夹具场景不适用，
    # 因为查询刻意不带 chapter/exam_point）。
    retriever = None
    ground_truth = None
    if args.fixture:
        retriever, fx = asyncio.run(build_fixture_retriever())
        ground_truth = {
            q["query_id"]: q["expect_relevant_chunk"] for q in fx.FIXTURE_QUERIES
        }
        if not args.queries:
            specs = [
                QuerySpec(
                    query=q["query"],
                    course_id=q.get("course_id", ""),
                    query_id=q["query_id"],
                )
                for q in fx.FIXTURE_QUERIES
            ]
        print(f"[夹具] 注入 {len(fx.FIXTURE_CHUNKS)} 条切片 / "
              f"{len(ground_truth)} 条查询真值")

    try:
        dataset = build_golden(
            specs, backend, args.recall_depth, stats,
            retriever=retriever, ground_truth=ground_truth,
        )
    finally:
        backend.close()

    if args.report:
        print_report(stats, args.recall_depth)
        return 0

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    header = {
        "queries": stats.queries,
        "recall_depth": args.recall_depth,
        "backend": backend.name,
        "stub": stats.stub_used,
        "distribution": distribution_report(stats),
    }

    if stats.stub_used:
        header["warning"] = (
            "logit 由 stub 后端哈希生成，无物理意义，禁止用于 tau/beta 标定。"
        )

    out.write_text(
        json.dumps(dataset, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print_report(stats, args.recall_depth)
    print(f"已写出：{out}（{len(dataset)} 条 query）")
    print()
    print("下一步——把真实数据喂进已通过平价测试的标定脚本：")
    print(f"  python scripts/eval_rerank_params.py {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
