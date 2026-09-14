# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""向量库连接层：租户作用域（TenantScope）+ ScopedQdrantClient + 进程内兜底。

隔离为什么必须有独立的"作用域"层
----------------------------------
早期版本是每个调用方自己拼 `models.Filter(must=[...])`。这在单租户下没问题，
但把"有没有加租户条件"这件事交给了**每一次调用的人**——漏一次就是跨租户泄漏，
而且没有任何类型系统或静态检查能拦住它（payload 过滤属于运行期行为）。

本模块把隔离收成三层：

1. **作用域下沉**：过滤仍在数据层发生，应用层连"看见"的机会都没有；
2. **作用域不可绕过**：Qdrant 客户端由 `ScopedQdrantClient` 独占，
   所有读写都必须先构造 `TenantScope`，过滤条件由固定的构造器生成——
   调用方只能传"值"，传不了"条件"，从接口形状上取消漏判空间；
3. **失败关闭**：多租户模式下租户缺失时**拒绝执行**，而不是放宽为全库。

拓扑策略：默认单集合 + 租户硬过滤
----------------------------------
**刻意不默认做"每租户一个 Collection"**：每个 Collection 都有独立的 HNSW
图索引与 payload 结构，1C2G / 2C4G 上开十个就能把 2G 内存吃穿。三档能力
按部署规模逐步开放：

- 默认        —— 单集合 + 租户 payload 过滤（已足够挡住"应用 Bug 泄露"，
                且配合 payload index 后按 tenant 过滤是索引命中的，不是全表扫）
- Shard Key  —— `QDRANT_TENANT_SHARDING=1`：集合按租户分片，读写只落到该
                租户的 shard，物理上更彻底（须在**建集合时**启用）
- 独立分仓    —— `QDRANT_DEDICATED_TENANTS=org-a,org-b`：名单内租户单独起一个
                `tenant_<id>` 集合。留给私有化高价值客户，不在 SHARED 场景默认开

拓扑是**基础设施决策**，与 `MULTI_TENANT_MODE` 同口径走环境变量，
刻意不做后台热开关：运行中换集合会让已有数据的归属在请求之间突变。
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Protocol

from app.core.config import get_settings
from app.core.tenancy import DEFAULT_TENANT, multi_tenant_enabled

_logger = logging.getLogger("app.db.vector_store")

TENANT_FIELD = "tenant_id"
COURSE_FIELD = "course_id"

# 集合需要先在业务侧意识到这两个字段：没有 payload index 的字段做过滤，
# Qdrant 会退化成"取全量再逐条比对"，百万级时是分钟级延迟。
_INDEXED_FIELDS = (TENANT_FIELD, COURSE_FIELD)


class TenantScopeRequired(RuntimeError):
    """多租户模式下却拿不到租户——按"失败关闭"原则拒绝本次数据访问。"""


def dedicated_tenants() -> set[str]:
    """独立分仓白名单（私有化客户专用），默认空。"""
    return {t.strip() for t in os.getenv("QDRANT_DEDICATED_TENANTS", "").split(",") if t.strip()}


def sharding_enabled() -> bool:
    return os.getenv("QDRANT_TENANT_SHARDING", "").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class TenantScope:
    """一次数据访问的租户边界：集合归属 + 过滤值 + 分片键，三处必须一致。

    `collection` 与 `shard_key` 由 `resolve_scope` 统一裁决，调用方无权指定——
    否则"配了分仓但过滤走错集合"这种错法会比不隔离更危险。
    """

    tenant_id: str | None
    collection: str
    shard_key: str | None = None

    @property
    def active(self) -> bool:
        """是否施加租户过滤。

        只在多租户模式开启**且**解析出有效租户时施加。单租户模式下不加，
        保证存量数据（payload 里没有 tenant_id）默认部署下依然可见——
        否则升级即"数据全丢"，这是不可接受的回归。
        """
        return bool(self.tenant_id) and multi_tenant_enabled()

    def matches(self, payload: dict | None) -> bool:
        """内存兜底路径的等价判定，语义必须与 Qdrant Filter 完全一致。"""
        if not self.active:
            return True
        return str((payload or {}).get(TENANT_FIELD) or DEFAULT_TENANT) == self.tenant_id


def resolve_scope(tenant_id: str | None, *, base_collection: str) -> TenantScope:
    """把租户解析成访问边界：集合名与分片键由这里唯一决定。"""
    tenant = str(tenant_id or "").strip() or None
    if not tenant:
        return TenantScope(None, base_collection)
    if tenant in dedicated_tenants():
        # 独立分仓：数据落到专属集合，物理上与他人无关。
        # 仍保留 payload 租户过滤——同一份代码路径只走一套判定，
        # 且 payload 里本来就有 tenant_id，属于零成本的纵深防御。
        return TenantScope(tenant, f"tenant_{tenant}")
    return TenantScope(tenant, base_collection, tenant if sharding_enabled() else None)


def _tenant_filter_active(tenant_id: str | None) -> bool:
    """兼容旧口径（retriever 的 BM25 分区等处仍在用）。"""
    return bool(tenant_id) and multi_tenant_enabled()


class VectorStore(Protocol):
    async def upsert(self, point_id: str, vector: list[float], payload: dict) -> None: ...

    async def search(
        self,
        vector: list[float],
        top_k: int,
        course_id: str | None = None,
        tenant_id: str | None = None,
    ) -> list[tuple[str, float, dict]]: ...


class InMemoryVectorStore:
    """零依赖兜底实现：暴力余弦扫描，教学场景数据量级下完全够用。"""

    def __init__(self) -> None:
        self._points: dict[str, tuple[list[float], dict]] = {}
        self._lock = threading.Lock()

    async def upsert(self, point_id: str, vector: list[float], payload: dict) -> None:
        with self._lock:
            self._points[point_id] = (vector, payload)

    async def search(
        self,
        vector: list[float],
        top_k: int,
        course_id: str | None = None,
        tenant_id: str | None = None,
    ) -> list[tuple[str, float, dict]]:
        from app.services.rag import embedder

        with self._lock:
            items = list(self._points.items())
        scope = resolve_scope(tenant_id, base_collection="memory")
        # 租户先于课程收窄：租户是最外层边界
        results = []
        for pid, (vec, payload) in items:
            if not scope.matches(payload):
                continue
            if course_id and payload.get(COURSE_FIELD) != course_id:
                continue
            results.append((pid, embedder.cosine(vector, vec), payload))
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]


class ScopedQdrantClient:
    """Qdrant 的**唯一出口**：所有条件在这里集中生成，外部拼不出来。

    为什么值得单独一层
    ------------------
    "某某次查询的 Filter 拼接有漏"是合规审计最核心的质疑。把 filter 构造
    收进私有方法后，调用方最多只能决定**过滤值**（course_id），连"要不要
    加租户条件"都决定不了——这是"可能会有 bug"变成"不可能有这个 bug"。
    """

    # 允许外部按值过滤的字段白名单：想加字段必须改这里，属于刻意的设计压力
    _EXTRA_FIELDS = frozenset({COURSE_FIELD})

    def __init__(self, url: str, base_collection: str) -> None:
        from qdrant_client import AsyncQdrantClient  # 懒加载可选依赖

        self._client = AsyncQdrantClient(url=url)
        self._base_collection = base_collection
        self._prepared: set[str] = set()
        self._sharding_ok = True

    @property
    def base_collection(self) -> str:
        return self._base_collection

    def _require(self, scope: TenantScope) -> None:
        """失败关闭：多租户模式下拿不到租户就别查。

        放宽成全库是"宁可泄漏也要可用"的取舍——对涉及考题与客户数据的系统，
        这个方向是错的，宁可本次请求失败并报错。
        """
        if multi_tenant_enabled() and not scope.tenant_id:
            raise TenantScopeRequired(
                "多租户模式下访问向量库必须携带有效 tenant_id（失败关闭，不放宽为全库）"
            )

    def _conditions(self, scope: TenantScope, extra: dict[str, str] | None) -> list:
        from qdrant_client import models

        must: list = []
        if scope.active:
            must.append(
                models.FieldCondition(
                    key=TENANT_FIELD, match=models.MatchValue(value=scope.tenant_id)
                )
            )
        for key, value in (extra or {}).items():
            if key not in self._EXTRA_FIELDS:
                raise ValueError(
                    f"字段 {key} 不在过滤白名单内（{sorted(self._EXTRA_FIELDS)}）——"
                    "新增字段需先在 _EXTRA_FIELDS 与 payload index 里登记"
                )
            if value:
                must.append(
                    models.FieldCondition(key=key, match=models.MatchValue(value=value))
                )
        return must

    async def _with_shard(self, call, scope: TenantScope, **kwargs):
        """带分片键调用，失败则永久降级回常规过滤。

        分片要求集合**建的时候**就启用自定义分片；历史集合或旧客户端会直接
        报错（服务端 400 或客户端 TypeError）。此时**不能让整个检索挂掉**，
        降级为"单集合 + 租户过滤"——那本来就是当前的安全基线。
        """
        if scope.shard_key and self._sharding_ok:
            try:
                return await call(**kwargs, shard_key=scope.shard_key)
            except Exception as exc:
                self._sharding_ok = False
                _logger.warning(
                    "分片键 %s 不可用，降级为单集合 + payload 过滤: %s", scope.shard_key, exc,
                )
        return await call(**kwargs)

    # ------------------------------------------------------------ schema ----
    async def ensure(self, scope: TenantScope, dim: int) -> None:
        from qdrant_client import models

        key = f"{scope.collection}:{dim}"
        if key in self._prepared:
            return
        if not await self._client.collection_exists(scope.collection):
            from app.core import profiles

            # 部署档位 qdrant_on_disk（eco/standard=True）：索引与载荷落盘，
            # 内存压制在几十 MB——2G 小机上防 OOM Killer 的保命锁。
            on_disk = bool(profiles.effective()["qdrant_on_disk"])
            await self._client.create_collection(
                collection_name=scope.collection,
                vectors_config=models.VectorParams(
                    size=dim, distance=models.Distance.COSINE, on_disk=on_disk
                ),
                hnsw_config=models.HnswConfigDiff(on_disk=on_disk),
                on_disk_payload=on_disk,
            )
            # 租户/课程字段必须建索引：否则每次过滤都是全集合扫 match
            for field in _INDEXED_FIELDS:
                await self._client.create_payload_index(
                    collection_name=scope.collection,
                    field_name=field,
                    field_schema=models.PayloadSchemaType.KEYWORD,
                )
        self._prepared.add(key)

    # -------------------------------------------------------------- ops ----
    async def search(
        self,
        scope: TenantScope,
        vector: list[float],
        top_k: int,
        extra: dict[str, str] | None = None,
    ) -> list[tuple[str, float, dict]]:
        from qdrant_client import models

        self._require(scope)
        must = self._conditions(scope, extra)
        hits = await self._with_shard(
            self._client.search,
            scope,
            collection_name=scope.collection,
            query_vector=vector,
            limit=top_k,
            query_filter=models.Filter(must=must) if must else None,
        )
        return [(str(h.id), h.score, h.payload or {}) for h in hits]

    async def upsert(
        self, scope: TenantScope, points: list[tuple[str, list[float], dict]]
    ) -> None:
        from qdrant_client import models

        self._require(scope)
        vector_dim = len(points[0][1]) if points else 0
        if not points:
            return
        await self.ensure(scope, vector_dim)
        await self._with_shard(
            self._client.upsert,
            scope,
            collection_name=scope.collection,
            points=[
                models.PointStruct(id=pid, vector=vec, payload=payload)
                for pid, vec, payload in points
            ],
        )

    async def count(self, scope: TenantScope, extra: dict[str, str] | None = None) -> int:
        """按租户计数，运营后台 FinOps 与配额核算用。"""
        from qdrant_client import models

        self._require(scope)
        must = self._conditions(scope, extra)
        res = await self._with_shard(
            self._client.count,
            scope,
            collection_name=scope.collection,
            count_filter=models.Filter(must=must) if must else None,
            exact=True,
        )
        return int(getattr(res, "count", res) or 0)


class QdrantVectorStore:
    """业务侧入口：只认 tenant_id / course_id 的值，不认 Qdrant 条件。"""

    def __init__(self, url: str, collection: str) -> None:
        self._scoped = ScopedQdrantClient(url, collection)
        self._default_collection = collection

    def scope_for(self, tenant_id: str | None) -> TenantScope:
        return resolve_scope(tenant_id, base_collection=self._default_collection)

    @property
    def scoped(self) -> ScopedQdrantClient:
        """只读暴露，供运维脚本按租户统计；不允许绕过 TenantScope。"""
        return self._scoped

    async def upsert(self, point_id: str, vector: list[float], payload: dict) -> None:
        # 写入按 payload 里的租户路由——独立分仓名单内的租户必须落到自己的集合
        scope = self.scope_for((payload or {}).get(TENANT_FIELD))
        await self._scoped.upsert(scope, [(point_id, vector, payload)])

    async def search(
        self,
        vector: list[float],
        top_k: int,
        course_id: str | None = None,
        tenant_id: str | None = None,
    ) -> list[tuple[str, float, dict]]:
        return await self._scoped.search(
            self.scope_for(tenant_id), vector, top_k,
            extra={COURSE_FIELD: course_id} if course_id else None,
        )

    async def count(self, tenant_id: str | None = None, course_id: str | None = None) -> int:
        return await self._scoped.count(
            self.scope_for(tenant_id),
            extra={COURSE_FIELD: course_id} if course_id else None,
        )


_store: VectorStore | None = None


def get_vector_store() -> VectorStore:
    global _store
    if _store is not None:
        return _store
    settings = get_settings()
    if settings.qdrant_url:
        try:
            _store = QdrantVectorStore(settings.qdrant_url, settings.qdrant_collection)
            return _store
        except ImportError:
            pass
    _store = InMemoryVectorStore()
    return _store
