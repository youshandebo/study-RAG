"""为核心源码文件（.py/.ts/.tsx）自动注入版权指纹头注释（幂等：已有则跳过）。"""
from __future__ import annotations

import io
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent          # backend/
REPO = ROOT.parent                                             # 仓库根
HEADER_TEXT = "Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com"
TARGETS = [
    ROOT / "app",
    ROOT / "workers",
    ROOT / "scripts",
    REPO / "frontend" / "src",
]
EXCLUDE_PARTS = {"__pycache__", "node_modules", ".next", "static", "data", "fonts"}
EXCLUDE_FILES = {"layout.tsx"}  # layout.tsx 头部需保持 'use client' 指令在前，单独处理


def header_line(ext: str) -> str:
    prefix = "#" if ext == ".py" else "//"
    return f"{prefix} {HEADER_TEXT}\n"


def main() -> None:
    added = skipped = 0
    for base in TARGETS:
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in (".py", ".ts", ".tsx"):
                continue
            if any(part in EXCLUDE_PARTS for part in path.parts):
                continue
            if path.name in EXCLUDE_FILES:
                continue
            text = io.open(path, encoding="utf-8").read()
            if "Copyright (C) 2026" in text[:400]:
                skipped += 1
                continue
            lines = text.splitlines(keepends=True)
            insert_at = 0
            # 'use client' 指令必须保持在文件最前（Next.js 约定）
            if lines and lines[0].strip() == "'use client';":
                insert_at = 1
            lines.insert(insert_at, header_line(path.suffix) + ("\n" if insert_at else ""))
            io.open(path, "w", encoding="utf-8").write("".join(lines))
            added += 1
    print(f"headers added: {added}, already present: {skipped}")


if __name__ == "__main__":
    main()
