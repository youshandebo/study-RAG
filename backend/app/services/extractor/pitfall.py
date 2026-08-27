"""易错陷阱与概念混淆挖掘：从切片文本中定位「老师强调的扣分点」。"""
from __future__ import annotations

import re

from app.services.rag.chunker import Chunk

_TRIGGER = re.compile(r"[^。！？]*(?:不可|不能|切勿|别|误区|陷阱|易错|千万别|注意)[^。！？]*[。！？]?")

_DEFAULT_PITFALLS = [
    "等价无穷小代换必须整体成立，禁止分子分母各自随意丢项。",
    "无穷限反常积分与瑕积分需先区分类型，检查区间端点的连续性。",
]


def extract_from_chunks(chunks: list[Chunk]) -> list[str]:
    pitfalls: list[str] = []
    for c in chunks:
        for m in _TRIGGER.finditer(c.text):
            sentence = m.group(0).strip()
            if len(sentence) >= 8 and sentence not in pitfalls:
                pitfalls.append(sentence)
    return pitfalls or list(_DEFAULT_PITFALLS)


def default_pitfalls() -> list[str]:
    return list(_DEFAULT_PITFALLS)
