# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""启动探针：把"多副本 + 无 Redis"这类静默降级变成**拒绝启动**。

为什么需要它
------------
限流器（`security.SlidingWindowLimiter`）、计费账本（`billing.BillingLedger`）、
入库闸门（`gate.DistributedGate`）三者的降级策略是一致的：没有 `REDIS_URL` 就用
进程内实现。这个策略在单副本下完全正确（少一个依赖，部署更简单），但在多副本下
是**灾难**——每个副本各自记一份内存账本、各自持一把内存锁：

- 日消耗配额被放大 N 倍（N 个副本各刷各的上限）→ 上游账单被打穿；
- 入库并发闸门失效（N 副本 × 容量）→ 小机器同时解压 N 个 PDF 直接 OOM；
- 限流窗口互不可见 → 实际放行速率是配置值的 N 倍。

最危险的地方在于**它不报错**。运维只是漏了一个环境变量，服务照常起来、照常
"看起来在工作"，直到账单或内存出问题才暴露——那时候已经无法归因。

判定原则
--------
1. **多副本是物理事实，与 ENV 无关**：只要探测到 workers>1，缺 Redis 一律拒绝
   启动。不是"生产才拦"——开发环境起 4 个 worker 同样会脑裂，只是危害小。
2. **生产环境从严**：`ENV=production` 时即使单副本也拒绝（配合下面的逃生舱），
   因为单副本生产随时可能被扩容，而扩容那一刻没人会想起来补 Redis。
3. **配了却用不上等于没配**：`REDIS_URL` 有值但 `redis` 驱动缺失时，三方组件
   会各自 except 后静默回落——同样是"看起来配了"。严格环境下也拒绝启动。
4. **留一个显式逃生舱**：`ALLOW_PROCESS_LOCAL_STATE=1` 表示部署方**知悉**该风险
   并接受（例如单副本、流量可控的内部部署）。逃生舱只对单副本生效——多副本下
   不管怎么声明都拒绝，因为那不是配置取舍而是数据错误。

探测来源（任一命中即视为多副本）
--------------------------------
- `UVICORN_WORKERS`：本项目 compose 显式传入
- `WEB_CONCURRENCY`：uvicorn/gunicorn 的标准变量
- `REPLICAS`：K8s 部署可显式声明
- 启动参数 `--workers N` / `-w N`：直接 `uvicorn main:app --workers 4` 的场景
"""
from __future__ import annotations

import importlib.util
import logging
import os
import sys

_logger = logging.getLogger("app.core.boot_probe")

# 视为"严格环境"的取值：这些环境下所有静默降级都必须变成硬失败
_STRICT_ENVS = frozenset({"production", "prod"})

# 显式承认"已知本部署是单副本、可接受进程内状态"的开关
_ACK_ENV = "ALLOW_PROCESS_LOCAL_STATE"

# 降级后到底失去了什么——写进日志与异常消息，让运维不必翻代码就知道后果
_DEGRADED_CAPABILITIES = (
    "日消耗硬熔断（core/billing.py）将按副本各记一份，配额被放大 N 倍",
    "入库并发闸门（core/gate.py）退化为进程内信号量，多副本同时解压直接 OOM",
    "滑动窗口限流（core/security.py）窗口互不可见，实际放行速率是配置值的 N 倍",
)


def _truthy(raw: str | None) -> bool:
    return (raw or "").strip().lower() in ("1", "true", "yes", "on")


def _int_env(name: str) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw.isdigit():
        return 0
    return int(raw)


def _workers_from_argv(argv: list[str] | None = None) -> int:
    """从启动参数里取 worker 数（`--workers 4` / `-w 4` / `--workers=4`）。

    覆盖“直接 uvicorn --workers N 启动、既没写 compose 也没设环境变量”的场景——
    这正是最容易漏配的组合：命令行加了并发，环境变量却没人管。
    """
    args = list(sys.argv if argv is None else argv)
    for idx, arg in enumerate(args):
        if arg in ("--workers", "-w"):
            if idx + 1 < len(args) and args[idx + 1].strip().isdigit():
                return int(args[idx + 1])
        elif arg.startswith("--workers="):
            value = arg.split("=", 1)[1].strip()
            if value.isdigit():
                return int(value)
    return 0


def replicas_hint(argv: list[str] | None = None) -> int:
    """进程数上界估计：多来源取最大值，任何一个说了算。"""
    return max(
        _int_env("UVICORN_WORKERS"),
        _int_env("WEB_CONCURRENCY"),
        _int_env("REPLICAS"),
        _workers_from_argv(argv),
        1,
    )


def environment() -> str:
    for name in ("ENV", "APP_ENV", "ENVIRONMENT"):
        value = (os.getenv(name) or "").strip().lower()
        if value:
            return value
    return "development"


def redis_driver_available() -> bool:
    """只查驱动是否可导入，不真的 import——避免启动期产生无用副作用。"""
    try:
        return importlib.util.find_spec("redis") is not None
    except (ImportError, ValueError):  # 父包异常 / 模块已被污染
        return False


def runtime_safety(argv: list[str] | None = None) -> dict:
    """只做判定、不抛异常：供 /health 与测试复用同一套口径。"""
    env = environment()
    replicas = replicas_hint(argv)
    redis_url = (os.getenv("REDIS_URL") or "").strip()
    driver = redis_driver_available()
    acknowledged = _truthy(os.getenv(_ACK_ENV))
    strict = env in _STRICT_ENVS
    # 配了地址但驱动缺失 = 三方组件会在 except 分支静默回落，等同于没配
    usable = bool(redis_url) and driver
    return {
        "environment": env,
        "replicas": replicas,
        "redis_configured": bool(redis_url),
        "redis_driver": driver,
        "state_backend": "redis" if usable else "process",
        "strict": strict,
        "acknowledged": acknowledged,
        "multi_replica": replicas > 1,
    }


def enforce_runtime_safety(argv: list[str] | None = None) -> dict:
    """在应用导入期调用。违反约束时 `SystemExit(1)`，让容器重启循环把问题暴露出来。

    返回判定摘要（放行时）供 /health 展示——"当前用的是 Redis 还是进程内状态"
    必须是可观测的，否则运维只能靠猜。
    """
    state = runtime_safety(argv)
    if state["state_backend"] == "redis":
        _logger.info(
            "启动探针通过：环境=%s，副本=%d，共享状态 Redis 可用",
            state["environment"], state["replicas"],
        )
        return state

    reason = (
        "REDIS_URL 未配置"
        if not state["redis_configured"]
        else "REDIS_URL 已配置但 redis 驱动缺失（pip install redis）"
    )
    detail = "\n".join(f"  - {item}" for item in _DEGRADED_CAPABILITIES)

    # 多副本：任何环境下都必须拒绝，逃生舱也不例外（那是数据错误，不是取舍）
    if state["multi_replica"]:
        raise SystemExit(
            f"[启动探针] 检测到多副本部署（副本数={state['replicas']}）但共享状态不可用：{reason}。\n"
            f"多副本下进程内状态会导致跨副本脑裂，以下能力将静默失效：\n{detail}\n"
            "修复方式（任选其一）：\n"
            "  1. 配置 REDIS_URL 指向共享 Redis（推荐，docker-compose.prod.yml 已内置）；\n"
            "  2. 把并发降为单副本：UVICORN_WORKERS=1 且不传 --workers。"
        )

    # 生产单副本：默认也拒绝——随时可能被扩容，而扩容时没人会想起补 Redis
    if state["strict"] and not state["acknowledged"]:
        raise SystemExit(
            f"[启动探针] 生产环境（ENV={state['environment']}）缺少共享状态：{reason}。\n"
            "单副本生产同样拒绝启动：扩容那一刻不会有人记得补 Redis，而那时已经脑裂。\n"
            f"以下能力当前只能降级运行：\n{detail}\n"
            "修复方式（任选其一）：\n"
            "  1. 配置 REDIS_URL（推荐）；\n"
            f"  2. 若确为单副本、流量可控且接受上述风险，显式设置 {_ACK_ENV}=1 表示知悉。"
        )

    if state["strict"]:
        _logger.warning(
            "启动探针：生产环境以进程内状态运行（已由 %s=1 显式确认）。降级能力：%s",
            _ACK_ENV, "；".join(_DEGRADED_CAPABILITIES),
        )
    else:
        _logger.warning(
            "启动探针：未配置 REDIS_URL，%s 环境单副本下使用进程内状态（限流/账本/闸门"
            "仅在本进程生效）。多副本部署前必须配置 Redis。",
            state["environment"],
        )
    return state
