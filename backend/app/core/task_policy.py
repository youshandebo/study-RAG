# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""入库任务的弹性策略：三层配置模型（档位默认 → 面板热调 → 内存保护层）。

为什么不能把参数写死在代码里
----------------------------
1C2G 只是**开箱安全底线**，不是目标部署形态。同一份代码会跑在 2C4G 的开发机、
8C16G 的校内服务器、以及接入高性能外部 ASR 的环境上。若并发槽位与收尸阈值
是常量，就会出现两个方向的错：

- 大机器上资源被软件人为掐死（并发恒为 1，8 核空转）
- 弱网/慢 ASR 环境下，固定阈值把"正在努力算"的长任务误判为僵尸并杀掉

三层模型
--------
1. **底层默认**：按部署档位（eco/standard/performance）给安全初值。
2. **控制层热调**：管理员在面板改 `runtime_config.tasks`，落盘即生效，
   无需重启进程——所有读取点都是"每次现读"，不缓存。
3. **保护层探针**：宿主机可用物理内存低于安全线时，无视管理员配置强制把
   并发降到 1。管理员可能误调，OOM 却是整站崩溃——这一层必须自动兜住。

保护层为什么只在内存不足时降级、而不自动扩容
--------------------------------------------
扩容需要知道"还有多少余量"（CPU 核数、ASR 配额、Embedding 限流），系统没有
这些信息，猜错就是过载；降级只需要知道"快死了"，这个信号是确定的。
"""
from __future__ import annotations

# ---- 第一层：档位默认 ----------------------------------------------------
# eco 是 1C2G 的开箱底线：串行入库，保证主聊天 API 永远拿得到 CPU 时间片。
# performance 放宽并发与收尸阈值：机器更强、且通常配了外部高速 ASR。
DEFAULTS_BY_PROFILE: dict[str, dict[str, int]] = {
    "eco": {"ingest_max_concurrency": 1, "task_zombie_timeout_s": 600, "task_phase_timeout_s": 300},
    "standard": {"ingest_max_concurrency": 2, "task_zombie_timeout_s": 600, "task_phase_timeout_s": 300},
    "performance": {"ingest_max_concurrency": 4, "task_zombie_timeout_s": 900, "task_phase_timeout_s": 300},
}

# ---- 边界约束：面板可以传任何数字，越界一律夹紧 ---------------------------
# 收尸下限 120s 是硬底线：再短就会把"正常但慢"的任务误杀，比僵尸更糟。
RANGES: dict[str, tuple[int, int]] = {
    "ingest_max_concurrency": (1, 8),
    "task_zombie_timeout_s": (120, 3600),
    "task_phase_timeout_s": (60, 3600),
}

# ---- 第三层：安全内存线 --------------------------------------------------
# 低于此值强制串行。150MB 是"还够处理一个切片批次"的量级，不是精确阈值——
# 目的是在管理员误调到 8 时避免整进程被 OOM killer 带走。
GUARDRAIL_MIN_FREE_MB = 150.0


def available_memory_mb() -> float | None:
    """宿主机可用物理内存（MB）。**探测不到返回 None**（不可观测就不降级）。

    为什么不做成"探测失败就按最坏情况处理"：那会让任何非 Linux/Windows 环境
    （如某些容器裁剪过的 /proc）永远串行，把一个可观测性问题变成性能事故。
    """
    try:  # Linux：MemAvailable 已扣除可回收缓存，比 MemFree 更贴近真实可用量
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return float(line.split()[1]) / 1024.0
    except OSError:
        pass

    try:  # Windows：GlobalMemoryStatusEx
        import ctypes

        class _MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = _MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        return float(stat.ullAvailPhys) / (1024.0 * 1024.0)
    except Exception:
        return None


def _clamp(key: str, value: float) -> int:
    lo, hi = RANGES[key]
    return int(max(lo, min(hi, value)))


def effective() -> dict:
    """解析最终生效值：档位默认 → 面板覆盖（夹紧）→ 内存保护层（可降级）。

    返回额外诊断字段：`profile`（档位）、`free_memory_mb`、`guardrail_applied`。
    降级必须**可见**——默默把 8 改成 1 而不留痕迹，运维会以为配置没生效。
    """
    from app.core import profiles, runtime_config

    profile = str(profiles.effective().get("profile") or "standard")
    value = dict(DEFAULTS_BY_PROFILE.get(profile, DEFAULTS_BY_PROFILE["standard"]))

    section = runtime_config.get_runtime_config().get("tasks") or {}
    if isinstance(section, dict):
        for key in RANGES:
            raw = str(section.get(key) or "").strip()
            if not raw:
                continue
            try:
                value[key] = _clamp(key, float(raw))
            except (TypeError, ValueError):
                continue  # 非法值忽略，保持上一层的取值

    free = available_memory_mb()
    guardrail = False
    if free is not None and free < GUARDRAIL_MIN_FREE_MB and value["ingest_max_concurrency"] > 1:
        value["ingest_max_concurrency"] = 1
        guardrail = True

    return {
        **value,
        "profile": profile,
        "free_memory_mb": free,
        "guardrail_applied": guardrail,
        "ranges": {k: {"min": v[0], "max": v[1]} for k, v in RANGES.items()},
    }
