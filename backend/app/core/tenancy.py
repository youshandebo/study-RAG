# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""租户（Tenant）上下文：知识库的**最外层**隔离边界。

为什么 course_id 不够
---------------------
`course_id` 是**课程**级作用域，它假设"所有能看到这个 course_id 的调用方
都已经被授权"。在单机构部署里这个假设成立；但在 README 宣称的
"SaaS 运营版 · 多租户运营"场景下它不成立——租户 A 只要请求里带上租户 B
的 `course_id`，就能直接读到 B 的知识库（`ingest`/`chat` 的 course_id 都是
客户端传参，服务端从不校验归属）。

结论：**授权边界必须是服务端权威、客户端不可指定的一等公民**，
这一层就是 tenant。

三条不可越狱规则
----------------
1. **来源权威**：tenant_id 只从服务端可信来源解析（JWT 签名载荷 → 用户记录），
   **绝不**读请求体 / 查询参数 / 请求头 / Cookie 里的任何租户字段。
2. **注入靠下**：过滤发生在数据层——Qdrant 走 `must` 硬过滤，内存向量库走
   等价的子集收窄，BM25 倒排走租户分区。检索/向量化/排序**全部看不到**跨租户数据，
   而不是"查出来再在应用层丢掉"（后者一旦某个分支漏判就是数据泄漏）。
3. **失败关闭**：多租户模式开启但租户解析不出结果时，**拒绝查询**而不是
   放宽为全库——默认必须是安全的那一侧。

兼容性
------
默认**关闭**（`MULTI_TENANT_MODE` 未设置）：所有数据归入 `public` 单租户，
行为与引入本模块前逐位一致，存量部署零影响。

开启时需先给存量数据打标，见 `scripts/migrate_tenants.py`；
未打标的旧数据在开启后将不可见（这是有意的——宁可看不见，也不能串租户）。
"""
from __future__ import annotations

import logging
import os
import re

_logger = logging.getLogger("app.core.tenancy")

# 单租户模式下的统一归属；也是存量数据的迁移目标
DEFAULT_TENANT = "public"

# 租户 id 允许的字符集：够用且便于放进 payload / Redis key / 日志
_TENANT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class TenantError(Exception):
    """租户上下文缺失或非法——调用方应回 403，不得降级为全库查询。"""


def multi_tenant_enabled() -> bool:
    """多租户隔离是否开启。

    唯一事实源是环境变量 `MULTI_TENANT_MODE`（1/true/yes/on 开启）。
    刻意**不**做管理后台热开关：租户是隔离开关，运行中切换会让
    "已有数据的归属语义"在请求之间突变（前一个请求按 public 写入，
    后一个按租户读取），这种不一致比"改配置要重启"危险得多。
    切换时机应当是受控的部署动作。
    """
    return os.getenv("MULTI_TENANT_MODE", "").strip().lower() in ("1", "true", "yes", "on")


def is_valid_tenant(tenant_id: str) -> bool:
    return bool(tenant_id) and bool(_TENANT_RE.match(tenant_id or ""))


def normalize_tenant(raw: str | None) -> str:
    """规范化：非法或空值一律归入默认租户。

    仅在**服务端可信来源**（用户记录字段）上使用。请求体里的值不要走这里，
    那会掩盖越权尝试——越权输入应当被拒绝而不是被"宽容地规范化"。
    """
    value = (raw or "").strip()
    return value if is_valid_tenant(value) else DEFAULT_TENANT


def resolve_tenant(user) -> str:
    """从请求级用户上下文解析租户（本模块唯一的对外入口）。

    - 多租户关闭 → 默认租户（行为与引入前一致）
    - 匿名用户   → 默认租户（开放演示模式无租户语义）
    - 登录用户   → 其记录上的 tenant_id；缺失时**拒绝**而非放行

    失败关闭（fail-closed）是有意的：多租户模式下拿不到租户，意味着
    鉴权链路出了问题，此时放行等于把全库暴露给一个身份不明的请求。
    """
    if not multi_tenant_enabled():
        return DEFAULT_TENANT
    if user is None or getattr(user, "anonymous", False):
        return DEFAULT_TENANT
    tenant = normalize_tenant(getattr(user, "tenant_id", None))
    if tenant == DEFAULT_TENANT:
        # 登录用户却没有租户归属：可能是旧 token / 迁移遗漏
        _logger.warning(
            "多租户模式下用户 %s 无租户归属，按默认租户处理",
            getattr(user, "id", "?"),
        )
    return tenant


def tenant_of_chunk_payload(payload: dict) -> str:
    """读切片 payload 的租户归属；缺失视为默认租户（存量数据）。"""
    return normalize_tenant(str((payload or {}).get("tenant_id") or ""))
