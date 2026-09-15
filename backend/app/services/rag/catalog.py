# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""知识库目录版本号：让多副本下各 Worker 知道"自己的内存索引过期了"。

问题
----
`HybridRetriever` 的 `_chunks` 与 BM25 倒排索引都在**进程内存**里。多 Worker 部署时，
Worker A 入库的切片不会出现在 Worker B 的倒排表里——B 的关键词通道看不到这条数据，
只能靠 Dense（Qdrant 集中存储）召回。表现为"长尾关键词完全匹配的切片位次抖动"。

为什么不用 Pub/Sub 广播
----------------------
"发布一条失效消息让各 Worker 清缓存"看着最直观，但有一个不可回避的缺陷：
**消息会丢**。

- Worker 在广播期间正在重启 / 扩容中 → 没订阅上 → 永远不知道索引变了；
- 网络抖动导致的丢包在 Pub/Sub 语义下没有重投；
- 而"索引落后"不是一次性错误，是**持续的错误结果**（该命中的切片长期不参与打分）。

也就是说 Pub/Sub 需要额外的兜底机制才能自愈，那不如直接用兜底机制本身。

本模块的做法：Redis 里放一个**单调递增的版本号**（每个租户一个），写路径 INCR，
读路径拿版本号与本地已同步版本比对，不一致就重载。好处：

- **自愈**：任何原因导致落后（重启、丢包、扩容），下一次查询比对版本号就能发现；
- **无需订阅生命周期管理**：不占用长连接，也不怕"订阅还没建立"的窗口期；
- **天然节流**：调用方按间隔检查（见 `retriever.CATALOG_CHECK_INTERVAL_S`），
  不是每个请求都打 Redis。

未配置 Redis 时不启用（返回 0）：单进程部署本就没有跨进程同步问题，
这与项目其余可选依赖"缺失即降级"的口径一致。
"""
from __future__ import annotations

import logging
import os

_logger = logging.getLogger("app.services.rag.catalog")

_SCOPE_PREFIX = "rag:catalog"


def _client():
    """惰性取共享 Redis；未配置或不可用时返回 None。"""
    url = (os.getenv("REDIS_URL") or "").strip()
    if not url:
        return None
    try:
        import redis

        client = redis.Redis.from_url(url, decode_responses=True, socket_timeout=1.0)
        client.ping()
        return client
    except Exception as exc:  # noqa: BLE001 - 降级为"无同步"，不影响单进程正确性
        _logger.warning("目录版本号不可用（Redis 连接失败，多副本索引同步关闭）: %s", exc)
        return None


def current_version(tenant_id: str | None) -> int:
    """当前版本号；无 Redis（或键不存在）返回 0 = "无同步能力"。"""
    client = _client()
    if client is None:
        return 0
    try:
        return int(client.get(f"{_SCOPE_PREFIX}:{tenant_id or 'public'}") or 0)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("目录版本号读取失败: %s", exc)
        return 0


def bump(tenant_id: str | None) -> int:
    """写路径调用：标记该租户的目录已变化。失败只记日志（不影响主流程）。"""
    client = _client()
    if client is None:
        return 0
    try:
        return int(client.incr(f"{_SCOPE_PREFIX}:{tenant_id or 'public'}"))
    except Exception as exc:  # noqa: BLE001
        _logger.warning("目录版本号自增失败（其他副本可能延迟发现新切片）: %s", exc)
        return 0
