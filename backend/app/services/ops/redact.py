# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""导出脱敏：让 bad-case 可以放心交给客户，而不至于把凭证和隐私一起交出去。

为什么必须做
------------
bad-case 导出里包含**用户原始 Query**。真实用户会往对话框里粘 API Key、
手机号、身份证、甚至整段配置文件——这些一旦进入导出的 CSV，再由客户方
多人转发，就是一次数据泄露事故。而导出又是排查问题刚需，不能一刀切禁止。

因此策略是：默认脱敏 + 可配置（追加/关闭），且**在写入时就脱敏**，
而不是导出时才处理——后者意味着数据库里躺着明文，一次误操作就全泄漏。

配置
----
- `OPS_REDACT=0`        关闭脱敏（仅限内网排障时临时开，不建议长期关）
- `OPS_REDACT_EXTRA`    追加自定义正则，多条用 `;;` 分隔
  （不用 `|`——那是正则里的"或"，会把用户写的表达式悄悄改掉）
"""
from __future__ import annotations

import os
import re
from typing import Iterable

# 命中即替换：越靠前优先级越高
DEFAULT_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"sk-[A-Za-z0-9_\-]{8,}", "[REDACTED_KEY]"),
    (r"Bearer\s+[A-Za-z0-9._\-]{8,}", "Bearer [REDACTED_TOKEN]"),
    (r"(?i)\b(api[_-]?key|access[_-]?token|password|passwd|secret)\b\s*[:=]\s*\S+", r"\1=[REDACTED]"),
    (r"\b1[3-9]\d{9}\b", "[REDACTED_PHONE]"),
    (r"\b\d{17}[\dXx]\b", "[REDACTED_ID]"),
    (r"[\w.+-]+@[\w-]+\.[\w.]+", "[REDACTED_EMAIL]"),
)

_COMPILED: tuple[tuple[re.Pattern[str], str], ...] | None = None


def enabled() -> bool:
    return os.getenv("OPS_REDACT", "").strip().lower() not in ("0", "false", "no", "off")


def _patterns() -> tuple[tuple[re.Pattern[str], str], ...]:
    global _COMPILED
    if _COMPILED is not None:
        return _COMPILED
    rules: list[tuple[re.Pattern[str], str]] = []
    for pattern, repl in DEFAULT_PATTERNS:
        try:
            rules.append((re.compile(pattern), repl))
        except re.error:  # 内置规则不该出错，出错也不能让脱敏整体失效
            continue
    for raw in (os.getenv("OPS_REDACT_EXTRA", "") or "").split(";;"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            rules.append((re.compile(raw), "[REDACTED]"))
        except re.error:
            pass
    _COMPILED = tuple(rules)
    return _COMPILED


def redact(text: str | None, rules: Iterable[tuple[re.Pattern[str], str]] | None = None) -> str:
    """脱敏单段文本；未开启时原样返回。"""
    if not text:
        return ""
    if not enabled():
        return text
    out = text
    for pattern, repl in (_patterns() if rules is None else rules):
        out = pattern.sub(repl, out)
    return out


def reset_cache() -> None:
    """测试 / 配置热更新用：清掉已编译规则。"""
    global _COMPILED
    _COMPILED = None
