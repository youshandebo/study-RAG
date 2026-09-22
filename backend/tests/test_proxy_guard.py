"""前端 `/api/proxy` 越权转发守卫（结构性断言）。

为什么是结构性断言而不是行为测试：本仓库只有后端 pytest，前端没有单测运行器
（CI 的前端步骤是 tsc + next build）。所以沿用仓库既有的 `TestSourceGuards`
套路——用源码结构锁死"必须存在这道闸"，让它在 CI 的 `pytest tests` 步骤里真跑。

为什么必须有这道闸：`/api/proxy/[...path]` 是**全路径透传**（tail 直接拼到
UPSTREAM）。部署环境 UPSTREAM 是 `http://backend:8000/api/v1`，业务路径因此天然
被限制在 API 前缀内；但 tail 里出现 `..` 时，fetch/undici 会把它归一化掉，于是
形如 `/api/proxy/..%2fmetrics` 的请求可以逃出前缀，打到后端**根路径**的
`/metrics`、`/docs`、`/openapi.json` —— 这些端点按设计无鉴权，且后端只绑
127.0.0.1 回环，本就不该从公网可达。

判定为什么是"拒绝 `..` + 显式拒绝运维端点"双闸而不是白名单：upstream 已经带了
`/api/v1` 前缀，白名单要穷举十余个业务段，漏一个就是线上故障；而业务路径永远不会
含 `..`，也永远不是 `/metrics`，所以这两条断言零误伤。
"""

from __future__ import annotations

from pathlib import Path

# tests/ -> backend/ -> 仓库根
REPO_ROOT = Path(__file__).resolve().parents[2]
PROXY_ROUTE = (
    REPO_ROOT
    / "frontend"
    / "src"
    / "app"
    / "api"
    / "proxy"
    / "[...path]"
    / "route.ts"
)


class TestProxyForwardGuard:
    def test_proxy_route_exists(self) -> None:
        assert PROXY_ROUTE.is_file(), f"未找到前端代理路由：{PROXY_ROUTE}"

    def test_proxy_rejects_traversal_segments(self) -> None:
        """tail 含 `..` 会被 fetch 归一化后逃出 /api/v1 前缀。"""
        src = PROXY_ROUTE.read_text(encoding="utf-8")
        assert "'..'" in src or '".."' in src, (
            "代理必须拒绝含 `..` 的路径段：否则 tail 被归一化后可逃出 API 前缀，"
            "打到后端根路径的无鉴权端点（/metrics、/docs、/openapi.json）"
        )

    def test_proxy_denies_ops_endpoints(self) -> None:
        """运维端点即使前缀逻辑将来变了，也不得经代理对外。"""
        src = PROXY_ROUTE.read_text(encoding="utf-8")
        for endpoint in ("/metrics", "/docs", "/openapi.json"):
            assert endpoint in src, (
                f"代理必须显式拒绝运维端点 {endpoint}（按设计无鉴权，只应内网可达）"
            )
