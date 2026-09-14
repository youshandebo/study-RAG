# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""跨副本并发闸门（Distributed Gate）：进程内 `asyncio.Semaphore` 的分布式替代。

为什么不能继续用 asyncio.Semaphore
----------------------------------
信号量是**单个进程**的概念。K8s 里跑 4 个副本，档位写的是"最多同时 2 个入库任务"，
实际会跑 8 个——解析 PDF / 长录音这类 CPU 密集任务一旦并发，主聊天 API 拿不到
时间片，部署档位的保命锁形同虚设。

为什么用"带 TTL 的租约"，而不是"计数 + 释放"
--------------------------------------------
纯计数（INCR 占用 / DECR 释放）在进程被 kill 时永远不会 DECR：一次 OOM 就永久
少一个名额，攒够几次入库彻底停摆。租约（ZSET + 过期时间）天然自愈——持有者崩溃
后租约到点自动失效，名额自己回来，无需任何运维介入。

代价与对策
----------
租约会到期，长任务不续约就会被人"抢跑"。因此获取成功后启动心跳任务，
按 ttl/3 的频率续期，直到显式释放。

不可用时的行为
--------------
与 `SlidingWindowLimiter` / `BillingLedger` 同一口径：未配置 REDIS_URL 或 Redis
故障时退回进程内信号量——单副本语义完全正确，多副本才需要 Redis。

时钟假设：租约到期时间以各副本本地时钟计算，因此副本之间需走 NTP 同步。
偏移远小于 300 秒的租约长度时不影响正确性。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass

_logger = logging.getLogger("app.core.gate")

# 租约默认时长：远大于常规入库耗时；配合心跳，长任务不会被误判超时
DEFAULT_LEASE_TTL_S = 300.0
# 排队上限：宁可快速失败也不要让 HTTP 请求无限挂着
DEFAULT_ACQUIRE_TIMEOUT_S = 600.0

_POLL_MIN_S = 0.15      # 首次重试间隔
_POLL_MAX_S = 1.5       # 退避上限，避免高频轮询打爆 Redis

# 获取租约：先清掉过期租约，再看有没有名额。
# **必须一次原子做完**——"先读数量再写"在高并发下会被击穿。
_ACQUIRE_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[3])
if tonumber(redis.call('ZCARD', KEYS[1])) < tonumber(ARGV[1]) then
  redis.call('ZADD', KEYS[1], ARGV[3] + ARGV[2], ARGV[4])
  return 1
end
return 0
"""

# 续约：只在租约**还在**时延长。已过期说明名额已被别人占用，
# 此时再去续约等于抢别人的名额，必须停手。
_HEARTBEAT_LUA = """
if redis.call('ZSCORE', KEYS[1], ARGV[1]) then
  redis.call('ZADD', KEYS[1], ARGV[2], ARGV[1])
  return 1
end
return 0
"""

_RELEASE_LUA = """
return redis.call('ZREM', KEYS[1], ARGV[1])
"""


class GateTimeout(Exception):
    """排队超时：拿不到并发名额。调用方应回 503 而不是让请求挂着。"""


@dataclass
class GateStatus:
    backend: str        # redis | process
    capacity: int
    in_flight: int


class DistributedGate:
    """限量执行重型任务的并发闸门，多副本共享同一份名额。"""

    def __init__(
        self,
        name: str,
        capacity: int,
        *,
        lease_ttl_s: float = DEFAULT_LEASE_TTL_S,
        redis_url: str | None = None,
        namespace: str = "gate",
    ) -> None:
        self.name = name
        self.capacity = max(1, int(capacity))
        self.lease_ttl_s = float(lease_ttl_s)
        self._key = f"{namespace}:{name}"
        self._redis_url = redis_url
        self._redis: object | None = None      # None=未尝试 / False=不可用 / 客户端=可用
        self._local: asyncio.Semaphore | None = None
        self._beats: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------- backend ----
    async def _client(self):
        """惰性连接 Redis：构造时还没进事件循环，连早了会绑错 loop。"""
        if self._redis is not None:
            return self._redis or None
        url = (self._redis_url if self._redis_url is not None
               else os.getenv("REDIS_URL", "").strip())
        if not url:
            self._redis = False
            return None
        try:
            import redis.asyncio as aioredis

            client = aioredis.from_url(url, decode_responses=True, socket_timeout=1.0)
            await client.ping()
            self._redis = client
            _logger.info("并发闸门 %s 已接入 Redis（容量 %d）", self.name, self.capacity)
        except Exception as exc:
            _logger.warning(
                "REDIS_URL 已配置但连接失败，%s 闸门降级进程内信号量: %s", self.name, exc,
            )
            self._redis = False
        return self._redis or None

    def _semaphore(self) -> asyncio.Semaphore:
        if self._local is None:
            self._local = asyncio.Semaphore(self.capacity)
        return self._local

    def backend(self) -> str:
        """当前生效的后端。首次使用前尚未探测，按进程内口径回报。"""
        return "redis" if self._redis else "process"

    # -------------------------------------------------------- acquire ----
    async def try_acquire(self) -> str | None:
        """立即尝试获取名额，成功返回租约 token，没有名额返回 None（不阻塞等待的情况下）。"""
        token = uuid.uuid4().hex[:16]
        client = await self._client()
        if client is not None:
            try:
                granted = await client.eval(
                    _ACQUIRE_LUA, 1, self._key,
                    self.capacity, int(self.lease_ttl_s * 1000), int(time.time() * 1000), token,
                )
                return token if granted else None
            except Exception as exc:
                _logger.warning("Redis 闸门获取失败，本次降级进程内: %s", exc)
        sem = self._semaphore()
        if sem.locked():
            return None
        await sem.acquire()
        return token

    async def acquire(self, timeout_s: float = DEFAULT_ACQUIRE_TIMEOUT_S) -> str:
        """阻塞式获取，指数退避轮询；超时抛 `GateTimeout`。

        轮询而非 Pub/Sub：入库排队以秒乃至分钟计，轮询的延迟完全可以接受，
        换来的是不用管订阅连接的重连与消息丢失。
        """
        deadline = time.monotonic() + max(0.0, timeout_s)
        delay = _POLL_MIN_S
        while True:
            token = await self.try_acquire()
            if token:
                self._start_heartbeat(token)
                return token
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GateTimeout(f"等待 {self.name} 名额超过 {timeout_s:.0f} 秒")
            await asyncio.sleep(min(delay, remaining))
            delay = min(_POLL_MAX_S, delay * 1.5)

    async def release(self, token: str) -> None:
        """释放名额并停止心跳。token 为空视为无操作（幂等，便于 finally 里直接写）。"""
        if not token:
            return
        task = self._beats.pop(token, None)
        if task is not None:
            task.cancel()
        client = self._redis or None
        if client is not None:
            try:
                await client.eval(_RELEASE_LUA, 1, self._key, token)
                return
            except Exception as exc:
                _logger.warning("Redis 闸门释放失败，本次降级进程内释放: %s", exc)
        sem = self._semaphore()
        try:
            sem.release()
        except ValueError:
            # 名额是从 Redis 拿的、这里释放不到本地信号量，属预期情形
            pass

    # ------------------------------------------------------ heartbeat ----
    def _start_heartbeat(self, token: str) -> None:
        client = self._redis or None
        if client is None:      # 进程内信号量不需要续约
            return

        async def _beat() -> None:
            interval = max(1.0, self.lease_ttl_s / 3)
            while True:
                await asyncio.sleep(interval)
                try:
                    ok = await client.eval(
                        _HEARTBEAT_LUA, 1, self._key, token,
                        int((time.time() + self.lease_ttl_s) * 1000),
                    )
                except Exception as exc:
                    _logger.warning("%s 闸门心跳异常，停止续约: %s", self.name, exc)
                    return
                if not ok:
                    _logger.warning(
                        "%s 租约 %s 已过期（可能被其他副本回收），停止续约", self.name, token,
                    )
                    return

        self._beats[token] = asyncio.create_task(_beat())

    # ------------------------------------------------------ visibility ----
    async def status(self) -> GateStatus:
        """运营视角：当前有几个在途任务（Redis 模式下会先清掉过期租约）。"""
        client = await self._client()
        in_flight = 0
        if client is not None:
            try:
                await client.zremrangebyscore(self._key, "-inf", int(time.time() * 1000))
                in_flight = int(await client.zcard(self._key))
            except Exception:
                in_flight = -1
        else:
            sem = self._semaphore()
            in_flight = self.capacity - getattr(sem, "_value", 0)
        return GateStatus(backend=self.backend(), capacity=self.capacity, in_flight=in_flight)
