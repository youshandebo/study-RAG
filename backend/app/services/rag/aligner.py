"""音画时空切片对齐器：将 ASR 句级时间戳与板书序列在统一课堂时间轴上配对。"""
from __future__ import annotations

from app.services.rag.chunker import Chunk


def _clock_to_ms(clock: str) -> int:
    mm, ss = clock.split(":")
    return (int(mm) * 60 + int(ss)) * 1000


def align_boards(chunks: list[Chunk], board_count: int, lecture_duration_ms: int = 50 * 60 * 1000) -> list[Chunk]:
    """无显式打点时，假设板书按授课顺序近似均匀分布于时间轴，
    将每个切片与其时间窗覆盖的板书序号对齐（就近原则）。"""
    if board_count == 0:
        for c in chunks:
            c.board_index = None
        return chunks

    for c in chunks:
        mid = (_clock_to_ms(c.start) + _clock_to_ms(c.end)) // 2
        idx = round(mid / lecture_duration_ms * board_count)
        c.board_index = max(1, min(board_count, idx))
        if not c.board_caption:
            c.board_caption = f"板书第 {c.board_index:02d} 张"
    return chunks


def align_transcript_to_boards(board_timestamps_ms: list[int], segments_start_ms: list[int]) -> list[int]:
    """给定每张板书的真实落笔时间戳，为每个转录句找到其活跃板书（最近一次落笔）。"""
    aligned: list[int] = []
    for s in segments_start_ms:
        candidates = [i for i, t in enumerate(board_timestamps_ms) if t <= s]
        aligned.append(candidates[-1] if candidates else 0)
    return aligned
