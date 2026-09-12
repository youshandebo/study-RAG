# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""上下文装箱器：按 token 预算贪心选片 + 邻近切片动态扩展。

解决两个问题：
1. 静态 top_k 截断是"盲盒拼装"——堆密集长切片会顶爆模型上下文窗口（API 400），
   堆零碎短句又白白浪费预算。改为按剩余 token 预算逐条扣减。
2. ASR 按句窗切块后单句缺前后语义——切片 id 形如 `{audio_id}-c003`，
   可利用顺序在预算允许时向前后探测 c002 / c004 并入，补回上下文。

零外部依赖：token 估算复用 app.services.tokens（中英文字符加权近似）。
"""
from __future__ import annotations

from dataclasses import dataclass

from app.services.rag.chunker import Chunk
from app.services.tokens import estimate_tokens

# 生成回复预留：给模型输出留出的余量，避免 prompt 顶满导致被截断
DEFAULT_GENERATION_BUFFER = 1024
# 单条切片占预算上限，防止一条超长转录独吞整个窗口
DEFAULT_MAX_CHUNK_TOKENS = 3000


@dataclass
class PackResult:
    chunks: list[Chunk]
    used_tokens: int
    dropped: int          # 因预算不足被丢弃的候选数
    expanded: list[str]   # 因邻近扩展被并入的切片 id
    budget: int           # 本次可用上下文预算


def _token_of(chunk: Chunk) -> int:
    """切片进入 prompt 后的实际 token 数（与拼装格式保持一致）。"""
    return estimate_tokens(f"[{chunk.start}-{chunk.end}] {chunk.text}")


def _neighbor_ids(chunk_id: str) -> tuple[str, str] | None:
    """从 `{audio_id}-c003` 解析出前后邻居 id；格式不符返回 None。"""
    head, sep, tail = chunk_id.rpartition("-c")
    if not sep or not tail.isdigit():
        return None
    width = len(tail)
    seq = int(tail)
    if seq <= 1:
        return None
    return f"{head}-c{seq - 1:0{width}d}", f"{head}-c{seq + 1:0{width}d}"


def compute_budget(
    model_limit: int,
    system_prompt: str,
    query: str,
    history: list[dict] | None = None,
    generation_buffer: int = DEFAULT_GENERATION_BUFFER,
) -> int:
    """T_context = T_model_limit - T_system - T_query - T_history - T_generation_buffer"""
    used = estimate_tokens(system_prompt) + estimate_tokens(query)
    for m in history or []:
        used += estimate_tokens(str(m.get("content") or ""))
    return max(0, int(model_limit) - used - int(generation_buffer))


def pack_context(
    candidates: list[tuple[float, Chunk]],
    budget: int,
    skeleton: dict[str, Chunk] | None = None,
    expand_neighbors: bool = True,
    max_chunk_tokens: int = DEFAULT_MAX_CHUNK_TOKENS,
) -> PackResult:
    """按得分降序贪心装箱。

    - 优先保证高分切片入选（贪心，非背包最优解——检索场景下高分即高价值）
    - 单条超长切片按 max_chunk_tokens 截断，不整条丢弃
    - 预算有余时，为已入选切片补前后邻居（skeleton 提供全集索引）
    """
    skeleton = skeleton or {}
    chosen: list[Chunk] = []
    expanded: list[str] = []
    seen: set[str] = set()
    used = 0
    dropped = 0

    for _score, chunk in candidates:
        if chunk.id in seen:
            continue
        cost = _token_of(chunk)
        if cost > max_chunk_tokens:
            # 超长切片按字符比例截断，保留开头（ASR 语境下开头信息密度更高）
            ratio = max_chunk_tokens / max(cost, 1)
            cut = max(1, int(len(chunk.text) * ratio))
            chunk = _with_text(chunk, chunk.text[:cut])
            cost = _token_of(chunk)
        if used + cost > budget:
            dropped += 1
            continue
        chosen.append(chunk)
        seen.add(chunk.id)
        used += cost

    if expand_neighbors and budget > 0:
        used = _expand(chosen, seen, skeleton, budget, used, expanded)

    return PackResult(
        chunks=chosen, used_tokens=used, dropped=dropped, expanded=expanded, budget=budget
    )


def _with_text(chunk: Chunk, text: str) -> Chunk:
    if hasattr(chunk, "model_copy"):
        return chunk.model_copy(update={"text": text})
    from dataclasses import replace

    return replace(chunk, text=text)


def _expand(
    chosen: list[Chunk],
    seen: set[str],
    skeleton: dict[str, Chunk],
    budget: int,
    used: int,
    expanded: list[str],
) -> int:
    """为已入选切片补前后邻居，直到预算耗尽。

    只对有顺序血缘（`-cNNN`）的切片生效；邻居未入选且未超预算才并入。
    """
    for chunk in list(chosen):
        ids = _neighbor_ids(chunk.id)
        if not ids:
            continue
        for nid in ids:
            if nid in seen:
                continue
            neighbor = skeleton.get(nid)
            if neighbor is None:
                continue
            cost = _token_of(neighbor)
            if used + cost > budget:
                continue
            chosen.append(neighbor)
            seen.add(nid)
            expanded.append(nid)
            used += cost
    return used


def serialize(chunks: list[Chunk], caption_fallback: str = "课堂切片") -> str:
    """统一拼装格式；与 packer 内部的 token 口径保持一致。"""
    parts: list[str] = []
    for c in chunks:
        label = f"{c.board_caption}·" if c.board_caption else ""
        parts.append(f"[{label}{c.start}-{c.end}] {c.text}")
    return "\n---\n".join(parts)
