"""板书分栏与结构化对齐：将 VLM 抽取的板书要素归位到逻辑栏区（左推导 / 右结论）。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BoardRegion:
    column: str  # left | right
    content: str
    order: int


_COLUMN_HINTS = {
    "left": ("解", "因为", "设", "由", "故", "=", "$$"),
    "right": ("综上", "所以", "因此", "结论", "∎", "答"),
}


def split_columns(lines: list[str]) -> list[BoardRegion]:
    regions: list[BoardRegion] = []
    for i, line in enumerate(lines):
        col = "right" if any(h in line for h in _COLUMN_HINTS["right"]) else "left"
        if col == "left" and any(h in line for h in _COLUMN_HINTS["left"]):
            col = "left"
        regions.append(BoardRegion(column=col, content=line.strip(), order=i))
    return regions


def merge_regions(regions: list[BoardRegion]) -> str:
    left = [r.content for r in regions if r.column == "left"]
    right = [r.content for r in regions if r.column == "right"]
    merged = "\n".join(left)
    if right:
        merged += "\n—— 右栏结论 ——\n" + "\n".join(right)
    return merged
