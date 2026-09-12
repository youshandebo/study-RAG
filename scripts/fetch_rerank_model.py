#!/usr/bin/env python3
# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""拉取 Cross-Encoder 的 ONNX 权重与分词器，供 rerank 参数标定使用。

为什么单独一个脚本
------------------
标定 (tau, beta) 需要真实 Cross-Encoder 的 logit，这要求本地有模型权重。
手工去 HuggingFace 找"哪个仓库有现成 onnx"再对目录结构，是这条链路上
最容易出错、也最难复现的一步。把它固化成脚本，任何人都能一条命令拿到
同样的文件布局。

设计取舍：**不依赖 torch**
------------------------
本脚本只下载**已导出好的 ONNX**，不在本地做 PyTorch → ONNX 转换。
原因：torch 安装体积 2GB+，而转换本身对使用者没有价值。
若所选仓库没有现成 ONNX，脚本会明确报错并给出替代仓库建议，
而不是静默回退到"装 torch 吧"。

镜像优先级
----------
hf-mirror.com（国内可达）> huggingface.co。可用 --endpoint 覆盖。
"""
from __future__ import annotations

import argparse
import pathlib
import sys

# 已知可用的「已导出 ONNX」reranker 仓库，按体积从小到大。
# 这些仓库都同时提供 model.onnx 与 tokenizer 文件，下载即可用。
KNOWN_REPOS = {
    "small": "Xenova/bge-reranker-base",       # 社区常用，已含 onnx/ 子目录
    "tiny": "Xenova/bge-reranker-v2-m3",       # 多语言版，体积略大
}

DEFAULT_ENDPOINT = "https://hf-mirror.com"


def _apply_endpoint(endpoint: str) -> None:
    """镜像地址必须通过 HF_ENDPOINT 环境变量设置。

    huggingface_hub 1.x 移除了各 API 的 `endpoint` 参数，改为读环境变量。
    统一在这里设，保证 list / download / tokenizer 三处走同一镜像。
    """
    import os

    os.environ["HF_ENDPOINT"] = endpoint


def _require_hub():
    try:
        import huggingface_hub  # noqa: F401
    except ImportError:
        print(
            "[错误] 缺少 huggingface_hub。请先安装：\n"
            "       pip install huggingface_hub",
            file=sys.stderr,
        )
        raise SystemExit(2)

    from huggingface_hub import hf_hub_download, list_repo_files

    return hf_hub_download, list_repo_files


def list_onnx_candidates(repo_id: str, endpoint: str) -> list[str]:
    """列出仓库里所有 .onnx 与分词器文件，供用户确认结构。"""
    _apply_endpoint(endpoint)
    _hf_download, list_repo_files = _require_hub()

    try:
        files = list_repo_files(repo_id)
    except Exception as exc:
        print(f"[错误] 无法列出仓库 {repo_id}: {exc}", file=sys.stderr)
        raise SystemExit(3)
    return [f for f in files if f.endswith(".onnx") or "tokenizer" in f or f.endswith(".json")]


def pick_onnx(files: list[str]) -> str:
    """挑一个 ONNX 文件。优先级：量化版 > 非量化版 > 任意。"""
    onnx_files = [f for f in files if f.endswith(".onnx")]
    if not onnx_files:
        return ""
    for kw in ("int8", "quantized", "model_quantized"):
        for f in onnx_files:
            if kw in f.lower():
                return f
    # 优先根目录的 model.onnx，其次 onnx/ 子目录
    for f in onnx_files:
        if f.count("/") == 0:
            return f
    return sorted(onnx_files, key=len)[0]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="拉取 Cross-Encoder ONNX 权重与分词器")
    ap.add_argument("--repo", default=KNOWN_REPOS["small"],
                    help=f"HF 仓库 id，默认 {KNOWN_REPOS['small']}")
    ap.add_argument("--size", choices=sorted(KNOWN_REPOS), default="",
                    help="预置仓库别名：" + " / ".join(f"{k}={v}" for k, v in KNOWN_REPOS.items()))
    ap.add_argument("--dest", default="models/bge-reranker",
                    help="本地存放目录，默认 models/bge-reranker")
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT,
                    help=f"下载镜像，默认 {DEFAULT_ENDPOINT}")
    ap.add_argument("--list-only", action="store_true",
                    help="只列出仓库文件结构，不下载")
    args = ap.parse_args(argv)

    # --size 与默认 --repo 同时存在时会静默互相覆盖，这里显式判定优先级，
    # 并在两者都非默认时给出提示，避免用户以为 --repo 生效了。
    default_repo = KNOWN_REPOS["small"]
    if args.size:
        repo = KNOWN_REPOS[args.size]
        if args.repo != default_repo:
            print(f"[提示] 同时指定了 --repo 与 --size，按 --size 取 {repo}", file=sys.stderr)
    else:
        repo = args.repo
    print(f"仓库    : {repo}")
    print(f"镜像    : {args.endpoint}")

    files = list_onnx_candidates(repo, args.endpoint)
    onnx_file = pick_onnx(files)

    if args.list_only:
        print(f"\n共 {len(files)} 个候选文件，ONNX 选择: {onnx_file or '（无）'}")
        for f in sorted(files)[:40]:
            print(f"  {f}")
        return 0

    if not onnx_file:
        print(
            f"\n[错误] 仓库 {repo} 内没有 .onnx 文件。\n"
            "       本脚本刻意不做 torch → ONNX 转换（torch 体积 2GB+）。\n"
            "       请换用已导出 ONNX 的仓库，例如：\n"
            + "\n".join(f"         {k:6s} {v}" for k, v in KNOWN_REPOS.items()),
            file=sys.stderr,
        )
        return 4

    hf_hub_download, _ = _require_hub()
    _apply_endpoint(args.endpoint)
    dest = pathlib.Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)

    # tokenizer 必需文件：不同仓库命名不一，按实际存在的拉
    tokenizer_names = [
        f for f in files
        if f.split("/")[-1] in (
            "tokenizer.json", "tokenizer_config.json",
            "special_tokens_map.json", "vocab.txt", "sentencepiece.bpe.model",
        )
    ]

    targets = [onnx_file] + sorted(tokenizer_names)
    print(f"\nONNX    : {onnx_file}")
    print(f"分词器  : {len(tokenizer_names)} 个文件")
    print(f"目标    : {dest}")
    print("-" * 56)

    for name in targets:
        try:
            path = hf_hub_download(
                repo_id=repo, filename=name, local_dir=str(dest),
            )
            size_mb = pathlib.Path(path).stat().st_size / 1e6
            print(f"  [OK]   {name}  ({size_mb:.1f} MB)")
        except Exception as exc:
            print(f"  [失败] {name}: {exc}", file=sys.stderr)

    # 落盘路径要与 OnnxReranker 期望的一致：model_path 指向 onnx 文件，
    # tokenizer_dir 指向同目录。这里把 onnx 挪到目录根部，简化调用。
    found = list(dest.rglob("*.onnx"))
    if found:
        root_onnx = dest / "model.onnx"
        if not root_onnx.exists():
            import shutil

            shutil.copy2(found[0], root_onnx)
            print(f"\n已就位: {root_onnx}")

    print("-" * 56)
    print("下一步——用真实权重跑标定（夹具语料，无需真实视频库）：")
    print(f"  python scripts/extract_rerank_dataset.py --fixture "
          f"--model-path {dest}/model.onnx --tokenizer-dir {dest} "
          f"--out data/eval_golden_real.json")
    print(f"  python scripts/eval_rerank_params.py data/eval_golden_real.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
