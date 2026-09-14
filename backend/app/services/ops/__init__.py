# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""运营后台支撑层（P1-C）：租户台账、用量聚合、bad-case 收集。

模块划分
--------
- `store.py`  数据层：租户账户 / 充值流水 / 用量事件 / bad-case，含进程内兜底
- `redact.py` 导出脱敏：写入即脱敏，库里不留明文凭证与隐私

权限口径（重要）
----------------
- **平台超管**：管理口令换的 admin JWT（`api/v1/admin.py` 的 `require_admin`）。
  只有它能建租户、充值、跨租户排查。
- **租户管理员**：普通用户 JWT + `users.role = tenant_admin`。
  只能看**自己租户**的数据，且租户值一律由服务端从用户记录解析。
两者正交：租户管理员在任何情况下都不是平台超管。
"""
from __future__ import annotations

import logging

_logger = logging.getLogger("app.services.ops")

__all__ = ["store", "redact", "_logger"]
