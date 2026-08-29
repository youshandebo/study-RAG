# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""语义级多模态切片切分：把带时间戳的 ASR 转录按句群聚合为教学切片 Chunk。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class TranscriptSegment:
    start_ms: int
    end_ms: int
    text: str


@dataclass
class Chunk:
    id: str
    audio_id: str
    start: str
    end: str
    text: str
    board_index: int | None = None
    # 课程作用域元数据：检索强隔离 + 时序衰减的物理时间基准
    subject: str = "未分类"       # 学科：数学 / 物理 / …
    course_id: str = "default"    # 课程唯一标识，检索强制同作用域
    chapter: str = ""             # 章节
    lecture_date: str = ""        # 授课日期 ISO（YYYY-MM-DD），空=今天
    # 解法定版权威信号：显式声明取代时间衰减的隐式推断
    is_canonical: bool = False    # 该考点的"标准解法"定版切片
    method_version: int = 1       # 解法版本号，老师改进讲法时递增
    supersedes: str | None = None # 指向被本版本取代的旧切片 id
    board_caption: str = ""
    exam_point: str = ""
    difficulty: int = 3
    pitfalls: list[str] = field(default_factory=list)

    def to_payload(self) -> dict:
        return {
            "is_canonical": self.is_canonical,
            "method_version": self.method_version,
            "supersedes": self.supersedes,
            "subject": self.subject,
            "course_id": self.course_id,
            "chapter": self.chapter,
            "lecture_date": self.lecture_date,
            "audio_id": self.audio_id,
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "board_index": self.board_index,
            "board_caption": self.board_caption,
            "exam_point": self.exam_point,
            "difficulty": self.difficulty,
            "pitfalls": self.pitfalls,
        }


def _ms_to_clock(ms: int) -> str:
    total_s = ms // 1000
    return f"{total_s // 60:02d}:{total_s % 60:02d}"


def chunk_transcript(audio_id: str, segments: list[TranscriptSegment], window_chars: int = 120) -> list[Chunk]:
    """滑动句窗聚合：相邻句子拼至窗口上限，保留首尾时间戳供证据回放定位。"""
    chunks: list[Chunk] = []
    buf_text: list[str] = []
    buf_start = 0
    buf_end = 0
    seq = 0

    def flush() -> None:
        nonlocal seq, buf_text
        if not buf_text:
            return
        seq += 1
        chunks.append(
            Chunk(
                id=f"{audio_id}-c{seq:03d}",
                audio_id=audio_id,
                start=_ms_to_clock(buf_start),
                end=_ms_to_clock(buf_end),
                text="".join(buf_text),
            )
        )
        buf_text = []

    for seg in segments:
        if not buf_text:
            buf_start = seg.start_ms
        buf_end = seg.end_ms
        buf_text.append(seg.text)
        joined = "".join(buf_text)
        if len(joined) >= window_chars or re.search(r"[。！？!?]$|。$|？$", seg.text.strip()):
            flush()
    flush()
    return chunks
