#!/usr/bin/env python3
# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""多租户迁移：给存量切片的 payload 补上 `tenant_id` 归属。

为什么必须显式迁移
------------------
开启 `MULTI_TENANT_MODE=1` 后，检索的租户过滤是 `tenant_id == <本租户>`。
存量切片的 payload 里没有这个字段，于是**在开启那一刻集体不可见**——
对用户表现为"知识库全空了"。

这是**有意**的设计而非缺陷：宁可看不见，也不能因为"字段缺失就放行"
而让所有租户互相串数据。代价是切换前必须跑一次本脚本。

用法
----
    # 1) 先看要改什么（不写入）
    python scripts/migrate_tenants.py --dry-run

    # 2) 确认后执行（默认把所有无名切片归入 public 租户）
    python scripts/migrate_tenants.py

    # 3) 若要把存量数据整体划给某个机构
    python scripts/migrate_tenants.py --tenant org-a

    # 4) 单机内存向量库（未配置 QDRANT_URL）无持久 payload 可迁移
    python scripts/migrate_tenants.py            # 会直接说明并退出

注意
----
- 只处理向量库 payload。SQLite/Postgres 里存的是会话与资产元数据，不含切片。
- 迁移是**幂等**的：已有 tenant_id 的切片不会被覆盖，除非显式加 `--force`。
- `--force` + `--tenant X` 会把**全部**切片重划给 X，跨租户搬运数据前请三思。
"""
from __future__ import annotations

import argparse
import pathlib
import sys

_BACKEND = pathlib.Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.core.config import get_settings  # noqa: E402
from app.core.tenancy import DEFAULT_TENANT, is_valid_tenant  # noqa: E402


def _connect():
    settings = get_settings()
    url = (settings.qdrant_url or "").strip()
    if not url:
        return None, settings.qdrant_collection
    try:
        from qdrant_client import QdrantClient
    except ImportError:
        print("[错误] 未安装 qdrant-client，无法迁移 Qdrant 数据。")
        print("       请先 pip install qdrant-client")
        raise SystemExit(2)
    return QdrantClient(url=url), settings.qdrant_collection


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="给存量切片补 tenant_id")
    ap.add_argument("--tenant", default=DEFAULT_TENANT,
                    help=f"归入的租户 id（默认 {DEFAULT_TENANT}）")
    ap.add_argument("--force", action="store_true",
                    help="覆盖已有 tenant_id（默认只补缺失的）")
    ap.add_argument("--dry-run", action="store_true", help="只看统计，不写入")
    ap.add_argument("--batch", type=int, default=256, help="每批处理条数")
    args = ap.parse_args(argv)

    tenant = (args.tenant or "").strip() or DEFAULT_TENANT
    if not is_valid_tenant(tenant):
        print(f"[错误] 非法租户 id: {tenant!r}（仅允许字母/数字/下划线/短横线，1~64 位）")
        return 2

    client, collection = _connect()
    if client is None:
        print("=" * 68)
        print("未配置 QDRANT_URL → 当前使用进程内向量库，没有持久化 payload 可迁移。")
        print("")
        print("进程内向量库不落盘：开启多租户后，重新启动进程、重新上传素材即可，")
        print("所有新切片会自动带上服务端注入的 tenant_id（见 ingest._tag_scope）。")
        print("=" * 68)
        return 0

    try:
        exists = client.collection_exists(collection)
    except Exception as exc:
        print(f"[错误] 无法连接 Qdrant（{exc}）")
        return 2
    if not exists:
        print(f"[跳过] 集合 {collection!r} 不存在，无需迁移。")
        return 0

    total = 0
    missing: list = []
    already = 0
    offset = None
    while True:
        records, offset = client.scroll(
            collection_name=collection,
            limit=args.batch,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for rec in records:
            total += 1
            payload = rec.payload or {}
            has = bool(str(payload.get("tenant_id") or "").strip())
            if has and not args.force:
                already += 1
                continue
            missing.append(rec.id)
        if offset is None:
            break

    print("=" * 68)
    print(f"集合            : {collection}")
    print(f"切片总数        : {total}")
    print(f"已有租户归属    : {already}")
    print(f"待补 / 待覆盖   : {len(missing)}")
    print(f"目标租户        : {tenant}")
    print(f"覆盖已有归属    : {'是（--force）' if args.force else '否'}")
    print("=" * 68)

    if not missing:
        print("无需迁移。")
        return 0
    if args.dry_run:
        print("--dry-run：未写入任何变更。")
        return 0

    done = 0
    for i in range(0, len(missing), args.batch):
        batch = missing[i : i + args.batch]
        client.set_payload(
            collection_name=collection,
            payload={"tenant_id": tenant},
            points=batch,
        )
        done += len(batch)
        print(f"  已写入 {done}/{len(missing)}")

    print(f"\n迁移完成：{done} 条切片归入租户 {tenant!r}。")
    print("现在可以设置 MULTI_TENANT_MODE=1 并重启后端。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
