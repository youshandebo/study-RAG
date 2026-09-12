#!/usr/bin/env python3
# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""Rerank 门控参数 (tau, beta) 离线网格标定。

零第三方依赖，纯标准库。用于把 reranker 的 tau/beta 从"经验推测"变成
"客观度量"——这两个参数直接决定定版切片能否获得权威提振。

用法：
    python scripts/eval_rerank_params.py                       # 用内置黄金集
    python scripts/eval_rerank_params.py data/eval_golden.json  # 用自定义黄金集
    python scripts/eval_rerank_params.py --json                # 输出机器可读结果

黄金集 JSON 结构（每条 query 一组候选）：
    [{"query_id","query","candidates":[{"chunk_id","logit","is_canonical","is_relevant"}]}]

标定目标（三项约束，优先级从高到低）：
    1. false_boosts == 0        —— 无关定版切片绝不能被误提权（硬约束）
    2. MRR@k 相对 baseline 正收益 ≥ 0.03
    3. canonical_activation_rate ≥ 0.90 —— 相关定版切片要能吃到红利
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys
from dataclasses import dataclass, field

# ------------------------------------------------------------------ 数据结构 --

# 模糊带上下界：对抗样本的 sigmoid 若落在此区间，才真正对 tau 形成约束。
# 真实 bge-reranker 实测中这个区间是空的（无关定版分数几乎全为 0.00x），
# 说明模型本身的区分力已足够，tau 在这类语料上不可辨识。
FUZZY_LO, FUZZY_HI = 0.40, 0.60


@dataclass
class EvalCandidate:
    chunk_id: str
    logit: float
    is_canonical: bool
    is_relevant: bool


@dataclass
class EvalQuery:
    query_id: str
    query: str
    candidates: list[EvalCandidate] = field(default_factory=list)


# -------------------------------------------------------------------- 核心 --


def stable_sigmoid(z: float) -> float:
    """数值稳定的 Sigmoid：朴素 1/(1+exp(-z)) 在 z 很负时会 OverflowError。"""
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    exp_z = math.exp(z)
    return exp_z / (1.0 + exp_z)


def final_score(cand: EvalCandidate, tau: float, beta: float) -> float:
    """门控权威加权：仅当语义达标且为定版时提振。"""
    s_sem = stable_sigmoid(cand.logit)
    if cand.is_canonical and s_sem >= tau:
        return s_sem * (1.0 + beta)
    return s_sem


def rank_candidates(
    candidates: list[EvalCandidate], tau: float, beta: float
) -> list[tuple[EvalCandidate, float]]:
    scored = [(c, final_score(c, tau, beta)) for c in candidates]
    # 同分时用 chunk_id 兜底，保证结果可复现（不依赖输入顺序）
    scored.sort(key=lambda x: (-x[1], x[0].chunk_id))
    return scored


def evaluate_dataset(
    dataset: list[EvalQuery], tau: float, beta: float, top_k: int = 5
) -> dict[str, float]:
    total_rr = 0.0
    false_boosts = 0            # 无关定版被误提权
    activated = 0               # 相关定版成功提权
    relevant_canonical_total = 0
    ranked_queries = 0

    for item in dataset:
        if not item.candidates:
            continue
        ranked_queries += 1
        ranked = rank_candidates(item.candidates, tau, beta)[:top_k]

        for rank, (cand, _) in enumerate(ranked, start=1):
            if cand.is_relevant:
                total_rr += 1.0 / rank
                break

        for cand, _ in ranked:
            boosted = cand.is_canonical and stable_sigmoid(cand.logit) >= tau
            if cand.is_canonical and cand.is_relevant:
                relevant_canonical_total += 1
                if boosted:
                    activated += 1
            if boosted and not cand.is_relevant:
                false_boosts += 1

    mrr = total_rr / ranked_queries if ranked_queries else 0.0
    activation = (
        activated / relevant_canonical_total if relevant_canonical_total else 0.0
    )
    return {
        "mrr": round(mrr, 4),
        "false_boosts": false_boosts,
        "canonical_activation_rate": round(activation, 4),
        "ranked_queries": ranked_queries,
    }


def theta_separability(dataset: list[EvalQuery]) -> dict:
    """可解性诊断：`tau` 在这份数据上到底有没有约束力？

    **为什么必须单独诊断**（来自真实模型实测的教训）：
    在真实 bge-reranker 上跑夹具语料时发现，无关定版的 sigmoid 几乎全是
    0.00x —— 它们天然排在末尾，`false_boosts` 在任何 tau 下都为 0。
    此时「三条约束全部 PASS」是**假阳性**：标定看似成功，实际 tau 是自由变量，
    取 0.30 还是 0.80 对结果没有任何区别。

    合成数据里不存在这个问题（我按"负样本长尾抬升"的说法把对抗样本的
    分数造到了 0.4~0.6），所以只有真实数据才会暴露。判据：

      fuzzy_count == 0  →  tau 不可辨识。所谓"最优 tau"只是在 MRR 断点上
                          随 beta 漂移，不具备物理含义。
      fuzzy_count > 0   →  tau 有约束力，标定结果可采信。

    返回 fuzzy 带内对抗定版的 sigmoid 值，供人工复核。
    """
    fuzzy: list[tuple[str, str, float]] = []
    adversarial_total = 0
    for item in dataset:
        for c in item.candidates:
            if not (c.is_canonical and not c.is_relevant):
                continue
            adversarial_total += 1
            s = stable_sigmoid(c.logit)
            if FUZZY_LO <= s <= FUZZY_HI:
                fuzzy.append((item.query_id, c.chunk_id, round(s, 4)))
    return {
        "adversarial_total": adversarial_total,
        "fuzzy_count": len(fuzzy),
        "fuzzy_examples": fuzzy[:10],
        "tau_identifiable": bool(fuzzy),
    }


def grid_search(
    dataset: list[EvalQuery],
    tau_range: list[float],
    beta_range: list[float],
    top_k: int = 5,
    min_mrr_gain: float = 0.03,
) -> dict:
    """网格搜索 (tau, beta)。

    选优策略（修正版）：
      1. 先按 false_boosts 升序 —— 误提权是硬约束，任何误提权都不可接受
      2. 同为 0 误提权时按 MRR 降序
      3. 再按激活率降序 —— 同等 MRR 下让更多相关定版吃到红利
      4. 最后按 beta 升序、tau 降序取最保守解 —— 避免在指标相同时
         选中"更大提振 + 更高阈值"这种边界敏感参数

    原参考实现的问题：`if false_boosts <= min_false_boosts` 配合
    `min_false_boosts` 一旦被压到 0 就再也无法更新，导致 tau 网格的
    后续取值全部被跳过，实际只探索了极小一部分参数空间。
    """
    baseline = evaluate_dataset(dataset, tau=0.0, beta=0.0, top_k=top_k)
    separability = theta_separability(dataset)

    grids: list[tuple] = []
    for tau in tau_range:
        for beta in beta_range:
            m = evaluate_dataset(dataset, tau, beta, top_k=top_k)
            grids.append((m["false_boosts"], -m["mrr"], -m["canonical_activation_rate"], beta, tau, m))

    grids.sort(key=lambda g: g[:5])
    best = grids[0]
    best_tau, best_beta, best_metrics = best[4], best[3], best[5]
    mrr_gain = round(best_metrics["mrr"] - baseline["mrr"], 4)

    return {
        "best_tau": best_tau,
        "best_beta": best_beta,
        "baseline": baseline,
        "optimized": best_metrics,
        "mrr_gain": mrr_gain,
        "separability": separability,
        "passes": {
            "no_false_boosts": best_metrics["false_boosts"] == 0,
            "mrr_gain_ok": mrr_gain >= min_mrr_gain,
            "activation_ok": best_metrics["canonical_activation_rate"] >= 0.90,
            # tau 不可辨识时，即使前三条全过也不算通过——否则会输出一个
            # 没有物理依据的阈值让人写进生产配置
            "tau_identifiable": separability["tau_identifiable"],
        },
        "candidates": [
            {"tau": g[4], "beta": g[3], **g[5]} for g in grids[:10]
        ],
    }


# -------------------------------------------------------------- 内置黄金集 --
# 构造原则（踩过的坑，勿重蹈）：
#   A) 对抗样本必须"分数接近且可能压过正确项"，否则它本来就垫底，
#      任何 beta 都改不动排名 → false_boosts 恒 0、MRR 恒 1.0，梯度消失，
#      网格搜索无从区分参数优劣。
#   B) 教师定版的 logit 要**略低于**口语原声（这正是要解决的问题：
#      措辞凝练的定版在纯语义下被口语重合词挤到第二位），
#      这样 beta 才有机会把它推回首位，产生可测的 MRR 收益。
#   C) 对抗样本的 sigmoid 需落在 tau 的搜索区间内侧，才能真正检验阈值。

BUILTIN_GOLDEN: list[dict] = [
    {
        "query_id": "q001",
        "query": "什么是反常积分的比较审敛法？",
        "candidates": [
            # 口语原声：与提问用词重合度高，纯语义下排第一
            {"chunk_id": "lec03-c012", "logit": 1.60, "is_canonical": False, "is_relevant": True},
            # 教师定版：语义同样相关，但措辞凝练、用词重合低，纯语义排第二
            {"chunk_id": "lec03-canon-01", "logit": 1.35, "is_canonical": True, "is_relevant": True},
            # 对抗：另考点定版，语义中等（s≈0.69），高于部分 tau 取值
            {"chunk_id": "lec02-canon-99", "logit": 0.80, "is_canonical": True, "is_relevant": False},
        ],
    },
    {
        "query_id": "q002",
        "query": "抓大头的时候能不能直接把 ln x 丢掉？",
        "candidates": [
            {"chunk_id": "lec03-c021", "logit": 1.50, "is_canonical": False, "is_relevant": True},
            {"chunk_id": "lec03-canon-02", "logit": 1.28, "is_canonical": True, "is_relevant": True},
            {"chunk_id": "lec05-canon-07", "logit": 0.85, "is_canonical": True, "is_relevant": False},
        ],
    },
    {
        "query_id": "q003",
        "query": "p 积分判别法的口诀是什么",
        "candidates": [
            {"chunk_id": "lec04-canon-01", "logit": 1.45, "is_canonical": False, "is_relevant": False},
            {"chunk_id": "lec04-canon-01b", "logit": 1.42, "is_canonical": True, "is_relevant": True},
            {"chunk_id": "lec01-canon-88", "logit": 0.95, "is_canonical": True, "is_relevant": False},
        ],
    },
    {
        "query_id": "q004",
        "query": "瑕积分和反常积分有什么区别",
        "candidates": [
            {"chunk_id": "lec06-c011", "logit": 1.70, "is_canonical": False, "is_relevant": True},
            {"chunk_id": "lec06-canon-01", "logit": 1.40, "is_canonical": True, "is_relevant": True},
            {"chunk_id": "lec02-canon-31", "logit": 0.88, "is_canonical": True, "is_relevant": False},
        ],
    },
    {
        "query_id": "q005",
        "query": "等价代换在什么情况下不成立",
        "candidates": [
            {"chunk_id": "lec07-c045", "logit": 1.55, "is_canonical": False, "is_relevant": True},
            {"chunk_id": "lec07-canon-01", "logit": 1.32, "is_canonical": True, "is_relevant": True},
            {"chunk_id": "lec03-canon-77", "logit": 0.92, "is_canonical": True, "is_relevant": False},
        ],
    },
    {
        "query_id": "q006",
        "query": "变式训练里那个四分之一放缩怎么来的",
        "candidates": [
            {"chunk_id": "lec08-c052", "logit": 1.50, "is_canonical": False, "is_relevant": True},
            {"chunk_id": "lec08-canon-01", "logit": 1.20, "is_canonical": True, "is_relevant": True},
            {"chunk_id": "lec04-canon-12", "logit": 0.82, "is_canonical": True, "is_relevant": False},
        ],
    },
    {
        "query_id": "q007",
        "query": "课程里说的收敛性判断顺序",
        "candidates": [
            {"chunk_id": "lec09-c061", "logit": 1.65, "is_canonical": False, "is_relevant": True},
            {"chunk_id": "lec09-canon-01", "logit": 1.38, "is_canonical": True, "is_relevant": True},
            {"chunk_id": "lec01-canon-45", "logit": 0.90, "is_canonical": True, "is_relevant": False},
        ],
    },
    {
        "query_id": "q008",
        "query": "课后这个积分题为什么发散了",
        "candidates": [
            {"chunk_id": "lec10-c072", "logit": 1.58, "is_canonical": False, "is_relevant": True},
            {"chunk_id": "lec10-canon-01", "logit": 1.44, "is_canonical": True, "is_relevant": True},
            {"chunk_id": "lec06-canon-23", "logit": 0.86, "is_canonical": True, "is_relevant": False},
        ],
    },
    {
        "query_id": "q009",
        "query": "课上强调的那个最大误区",
        "candidates": [
            {"chunk_id": "lec11-c081", "logit": 1.62, "is_canonical": False, "is_relevant": True},
            {"chunk_id": "lec11-canon-01", "logit": 1.30, "is_canonical": True, "is_relevant": True},
            {"chunk_id": "lec05-canon-66", "logit": 0.78, "is_canonical": True, "is_relevant": False},
        ],
    },
    {
        "query_id": "q010",
        "query": "这个题第一步应该先看什么",
        "candidates": [
            {"chunk_id": "lec12-c092", "logit": 1.48, "is_canonical": False, "is_relevant": True},
            {"chunk_id": "lec12-canon-01", "logit": 1.25, "is_canonical": True, "is_relevant": True},
            {"chunk_id": "lec02-canon-54", "logit": 0.84, "is_canonical": True, "is_relevant": False},
        ],
    },
]


# ---------------------------------------------------------------- 载入/输出 --


def _guard_provenance(raw, path: pathlib.Path) -> bool:
    """数据来源闸门：拒绝把无物理意义的 logit 标定结果写进生产。

    `scripts/extract_rerank_dataset.py --allow-stub` 产出的黄金集带 `stub`
    标记，其 logit 是哈希派生的，标出来的 (tau, beta) 与真实模型无关。
    这里硬拦一道，避免"管线跑通了"被误读成"参数标定完成了"。

    抽取脚本把 `stub` 标记写进**每一条 query**，而不是文件头——黄金集文件
    经常被当作"一个候选数组"在人和脚本之间搬运，头部元信息容易在搬运中
    丢失；写进每条里，这份数据就永久自证来源。

    返回 True 表示数据不可采信，调用方必须直接终止——**连推荐参数都不能打印**。
    之前的版本只警告不终止，退化标定仍会吐出 `tau=0.30 beta=0.00` 这种
    看似合法的结果，一旦被复制进 runtime_config 就是静默事故。
    """
    if isinstance(raw, list) and any(
        isinstance(item, dict) and item.get("stub") for item in raw
    ):
        print(
            f"[拒绝] {path.name} 由 stub 后端产出，logit 无物理意义。\n"
            "       本次仅可验证管线连通性，不输出任何推荐参数。\n"
            "       真实标定请用 --model-path 指定 Cross-Encoder 后重新抽取。",
            file=sys.stderr,
        )
        return True
    if isinstance(raw, dict) and raw.get("stub"):
        print(f"[拒绝] {path.name} 含 stub 标记，结果不可采信。", file=sys.stderr)
        return True
    return False


def load_dataset(path: str | None) -> list[EvalQuery]:
    if not path:
        raw = BUILTIN_GOLDEN
    else:
        p = pathlib.Path(path)
        if not p.exists():
            print(f"[错误] 黄金集不存在: {p}", file=sys.stderr)
            raise SystemExit(2)
        raw = json.loads(p.read_text(encoding="utf-8"))
        if _guard_provenance(raw, p):
            raise SystemExit(3)

    dataset: list[EvalQuery] = []
    for item in raw or []:
        cands = [
            EvalCandidate(
                chunk_id=str(c.get("chunk_id") or ""),
                logit=float(c.get("logit") or 0.0),
                is_canonical=bool(c.get("is_canonical")),
                is_relevant=bool(c.get("is_relevant")),
            )
            for c in (item.get("candidates") or [])
        ]
        dataset.append(
            EvalQuery(
                query_id=str(item.get("query_id") or ""),
                query=str(item.get("query") or ""),
                candidates=cands,
            )
        )
    return dataset


def fmt_range(values: list[float]) -> str:
    return ", ".join(f"{v:g}" for v in values)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Rerank 门控参数 (tau, beta) 离线标定")
    ap.add_argument("golden", nargs="?", default=None, help="黄金集 JSON 路径（省略用内置）")
    ap.add_argument("--top-k", type=int, default=5, help="MRR@k 的 k，默认 5")
    ap.add_argument("--tau-min", type=float, default=0.30)
    ap.add_argument("--tau-max", type=float, default=0.80)
    ap.add_argument("--tau-step", type=float, default=0.02)
    ap.add_argument("--beta-min", type=float, default=0.0)
    ap.add_argument("--beta-max", type=float, default=0.60)
    ap.add_argument("--beta-step", type=float, default=0.02)
    ap.add_argument("--min-mrr-gain", type=float, default=0.03)
    ap.add_argument("--json", action="store_true", help="只输出 JSON 结果")
    args = ap.parse_args(argv)

    def _seq(lo: float, hi: float, step: float) -> list[float]:
        out: list[float] = []
        n = int(round((hi - lo) / step))
        for i in range(n + 1):
            out.append(round(lo + i * step, 6))
        return out

    dataset = load_dataset(args.golden)
    if not dataset:
        print("[错误] 黄金集为空", file=sys.stderr)
        return 2

    result = grid_search(
        dataset,
        _seq(args.tau_min, args.tau_max, args.tau_step),
        _seq(args.beta_min, args.beta_max, args.beta_step),
        top_k=args.top_k,
        min_mrr_gain=args.min_mrr_gain,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if all(result["passes"].values()) else 1

    b, o = result["baseline"], result["optimized"]
    print("=" * 62)
    print(f"黄金集：{len(dataset)} 条 query，MRR@{args.top_k}")
    print(f"tau 网格 [{fmt_range(_seq(args.tau_min, args.tau_max, args.tau_step))[:48]}...]")
    print(f"beta 网格 [{args.beta_min:g} ~ {args.beta_max:g}] step={args.beta_step:g}")
    print("=" * 62)
    print(f"{'':22s}{'baseline':>12s}{'optimized':>12s}")
    print(f"{'MRR':22s}{b['mrr']:>12.4f}{o['mrr']:>12.4f}")
    print(f"{'false_boosts':22s}{b['false_boosts']:>12d}{o['false_boosts']:>12d}")
    print(f"{'activation_rate':22s}{b['canonical_activation_rate']:>12.4f}{o['canonical_activation_rate']:>12.4f}")
    print("-" * 62)
    sep = result["separability"]
    print(f"可解性诊断：对抗定版 {sep['adversarial_total']} 条，"
          f"其中落在模糊带 [{FUZZY_LO:g},{FUZZY_HI:g}] 的 {sep['fuzzy_count']} 条")
    if not sep["tau_identifiable"]:
        print("  [!] 模糊带为空 → tau 不可辨识。所有对抗定版的 sigmoid 都远离")
        print("      阈值区间，false_boosts 在任何 tau 下都是 0。此时所谓")
        print("      「最优 tau」只是在 MRR 断点上随 beta 漂移，没有物理依据，")
        print("      **不要**把它写进 runtime_config。")
    print("-" * 62)
    print(f"最优参数：tau = {result['best_tau']:g}   beta = {result['best_beta']:g}")
    print(f"MRR 收益：{result['mrr_gain']:+.4f}（门槛 {args.min_mrr_gain:+.2f}）")
    print("-" * 62)
    for name, ok in result["passes"].items():
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}")
    print("=" * 62)

    ok_all = all(result["passes"].values())
    if ok_all:
        print("结论：参数可用。建议写入 runtime_config 的 rerank 段：")
        print(f'  "rerank": {{"enabled": "true", "tau": "{result["best_tau"]:g}", '
              f'"beta": "{result["best_beta"]:g}"}}')
    elif not sep["tau_identifiable"]:
        print("结论：beta 可用但 **tau 不可标定**——模型的区分力已经足够，")
        print("      tau 在这份数据上是自由变量。建议：")
        print(f"      1) beta 取 {result['best_beta']:g}（这部分是可靠的，MRR 收益 {result['mrr_gain']:+.4f} 有据）")
        print("      2) tau 保持保守默认值，或改由更贴近真实分布的语料重标")
    else:
        print("结论：当前黄金集下未找到满足全部约束的参数，请检查阈值设置或补充样本。")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
