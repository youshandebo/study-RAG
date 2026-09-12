# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""额度计费：**预冻结 + 终态结算**，按"有效产出"收费，绝不按"发起动作"收费。

为什么单独一层
--------------
会员档位（`membership.py`）管的是"能存多少 / 能多快问"，是静态配额；
本模块管的是"这一次提问消耗多少额度"，是动态结算。两者正交：档位决定
额度单价与上限，计费负责在真实用量上扣减。

核心不变式
----------
    任何一次请求，额度只可能落入三种终态之一，且只落一次：
        settled（按实际用量扣减） / released（全额退回） / expired（超时自动退）

预冻结 + 终态结算（而不是"先扣后退"或"用完再扣"）
--------------------------------------------------
- 纯预扣：流式中断时若不退款，用户为没拿到的答案付费 → 必须回头补退款逻辑。
- 纯后扣：流式"开个头就断"，模型已经烧了钱，平台收不到 → 可被白嫖。
- 本实现：请求开始即**冻结**一份上限额度（用户能看到"正在扣"，但没真扣）；
  在 `finally` 里按**实际有效 token** 决定 settle 还是 release。

这样"中断"不再是需要特判的异常路径——它只是"有效产出偏少"的一种情形，
结算逻辑天然覆盖。

有效 token 阈值（DEFAULT_MIN_BILLABLE_TOKENS）
---------------------------------------------
流式刚吐 3 个 token 就断，不该收费；吐了 500 字后用户嫌答得不好主动停，
该收费（模型成本已经发生）。50 token 是分界点：低于它视为"未成功交付"。

幂等与并发
----------
- `request_id` 是幂等键：重复调用同一 id 直接返回首次结果，不重复扣。
- 进程内用锁 + 已结算集合；配置 REDIS_URL 时走 Redis（`SETNX` 占位、
  `DECRBY` 原子扣减），多副本下同样正确。
- 冻结凭证带 `expires_at`：进程崩溃导致既没 settle 也没 release 时，
  由 `sweep_expired()` 兜底全额退回，**不制造悬空扣费**。
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

_logger = logging.getLogger("app.core.billing")

# 低于此 token 数视为"未成功交付"，全额退回
DEFAULT_MIN_BILLABLE_TOKENS = 50
# 冻结凭证有效期：超过则视为悬挂，由 sweep_expired 退回
DEFAULT_HOLD_TTL_S = 300.0


class HoldState(str, Enum):
    held = "held"          # 已冻结，等待结算
    settled = "settled"    # 已按实际用量扣减
    released = "released"  # 全额退回
    expired = "expired"    # 超时兜底退回


@dataclass
class Hold:
    """一次预冻结凭证。"""

    hold_id: str
    account: str
    amount: int                       # 冻结额度（上限）
    units: int = 1                    # 计费单位数（compare 的轨道数 = 多倍成本）
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    state: HoldState = HoldState.held
    charged: int = 0

    def __post_init__(self) -> None:
        if not self.expires_at:
            self.expires_at = self.created_at + DEFAULT_HOLD_TTL_S
        if self.units < 1:
            self.units = 1


@dataclass
class SettlementResult:
    hold_id: str
    state: HoldState
    charged: int          # 实际扣减
    refunded: int         # 退回
    reason: str
    idempotent: bool = False   # True = 本次是重复请求，未产生新扣费


def _idem_key(request_id: str) -> str:
    return "bill:idem:" + hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:24]


class BillingLedger:
    """额度账本：进程内实现 + 可选 Redis 后端（多副本共享）。

    未配置 Redis 时行为完全正确，只是多副本下各自记账——与项目其余
    可选依赖的处理口径一致（`SlidingWindowLimiter` 同款策略）。
    """

    def __init__(self, *, redis_scope: str = "bill") -> None:
        self._lock = threading.RLock()
        self._balances: dict[str, int] = {}
        self._holds: dict[str, Hold] = {}
        self._settled_ids: set[str] = set()
        self._redis_scope = redis_scope
        self._redis = self._init_redis()

    # -------------------------------------------------------- redis ----
    def _init_redis(self):
        try:
            import os

            url = os.getenv("REDIS_URL", "").strip()
            if not url:
                return None
            import redis  # 懒加载可选依赖

            client = redis.Redis.from_url(url, decode_responses=True)
            client.ping()
            return client
        except Exception:
            return None

    def _rkey(self, *parts: str) -> str:
        return ":".join((self._redis_scope, *parts))

    # ------------------------------------------------------- balance ----
    def balance(self, account: str) -> int:
        if self._redis is not None:
            try:
                raw = self._redis.get(self._rkey("bal", account))
                return int(raw) if raw is not None else 0
            except Exception as exc:
                _logger.warning("Redis 余额读取失败，降级进程内: %s", exc)
        with self._lock:
            return self._balances.get(account, 0)

    def grant(self, account: str, amount: int) -> int:
        """充值 / 发放额度（管理后台或兑换码通道调用）。"""
        amount = int(amount)
        if amount <= 0:
            return self.balance(account)
        if self._redis is not None:
            try:
                return int(self._redis.incrby(self._rkey("bal", account), amount))
            except Exception as exc:
                _logger.warning("Redis 充值失败，降级进程内: %s", exc)
        with self._lock:
            self._balances[account] = self._balances.get(account, 0) + amount
            return self._balances[account]

    # ---------------------------------------------------------- hold ----
    def reserve(
        self,
        account: str,
        amount: int,
        *,
        request_id: str = "",
        units: int = 1,
        ttl_s: float = DEFAULT_HOLD_TTL_S,
    ) -> Hold | None:
        """预冻结。余额不足返回 None（调用方应回 402 / 429）。

        `units` 是**计费单位数**——compare 并发 N 条模型轨道就传 N，
        让"多倍成本"在冻结阶段就体现为多倍占用，而非结算时才被发现。
        `amount` 是**单位额度**，最终冻结 amount * units。
        """
        amount = max(0, int(amount))
        units = max(1, int(units))
        total = amount * units

        if request_id and self.is_duplicate(request_id):
            # 重复请求：不重复冻结，返回一个零额度凭证供上层走幂等返回
            h = Hold(hold_id=f"dup-{request_id}", account=account, amount=0, units=units)
            h.state = HoldState.settled
            h.charged = 0
            return h

        if self._redis is not None:
            try:
                if int(self._redis.decrby(self._rkey("bal", account), total)) < 0:
                    self._redis.incrby(self._rkey("bal", account), total)  # 回滚
                    return None
            except Exception as exc:
                _logger.warning("Redis 预冻结失败，降级进程内: %s", exc)
        else:
            with self._lock:
                if self._balances.get(account, 0) < total:
                    return None
                self._balances[account] -= total

        hold = Hold(hold_id=uuid.uuid4().hex[:16], account=account, amount=total, units=units)
        hold.expires_at = hold.created_at + float(ttl_s or DEFAULT_HOLD_TTL_S)
        with self._lock:
            self._holds[hold.hold_id] = hold
        return hold

    def settle(
        self,
        hold: Hold,
        *,
        effective_tokens: int,
        request_id: str = "",
        reason: str = "",
    ) -> SettlementResult:
        """终态结算：按实际有效 token 决定扣多少、退多少。

        这是唯一会"真的扣钱"的入口，且对同一 hold 只生效一次。
        """
        with self._lock:
            current = self._holds.get(hold.hold_id, hold)
            if current.state != HoldState.held:
                return SettlementResult(
                    hold_id=current.hold_id, state=current.state, charged=current.charged,
                    refunded=current.amount - current.charged,
                    reason=f"已结算，忽略重复调用（原因: {current.state.value}）",
                    idempotent=True,
                )

            if effective_tokens < DEFAULT_MIN_BILLABLE_TOKENS:
                return self._release_locked(current, HoldState.released,
                                            f"有效产出不足（{effective_tokens} < "
                                            f"{DEFAULT_MIN_BILLABLE_TOKENS} token），全额退回")

            # 按 token 折算：冻结额度是上限，实际用量不超过它
            # 这里用"1 token ≈ 1 额度单位"的保守口径；接入真实计价表时改这一处即可
            charge = min(current.amount, int(effective_tokens))
            refund = current.amount - charge
            current.charged = charge
            current.state = HoldState.settled

        if refund > 0:
            self._refund(hold.account, refund)
        if request_id:
            self._mark_settled(request_id)
        _logger.info(
            "结算完成 hold=%s account=%s 扣 %d 退 %d（%s）",
            hold.hold_id, hold.account, charge, refund, reason or "正常",
        )
        return SettlementResult(
            hold_id=hold.hold_id, state=HoldState.settled,
            charged=charge, refunded=refund, reason=reason or "按实际用量结算",
        )

    def release(self, hold: Hold, *, reason: str = "") -> SettlementResult:
        """全额退回（未产出 / 异常路径）。"""
        with self._lock:
            current = self._holds.get(hold.hold_id, hold)
            if current.state != HoldState.held:
                return SettlementResult(
                    hold_id=current.hold_id, state=current.state, charged=current.charged,
                    refunded=0, reason="已结算，忽略重复 release", idempotent=True,
                )
            return self._release_locked(current, HoldState.released, reason or "主动退回")

    def _release_locked(self, hold: Hold, state: HoldState, reason: str) -> SettlementResult:
        hold.state = state
        hold.charged = 0
        refund = hold.amount
        if refund > 0:
            self._refund(hold.account, refund)
        _logger.info("退回 hold=%s account=%s 全额 %d（%s）", hold.hold_id, hold.account, refund, reason)
        return SettlementResult(
            hold_id=hold.hold_id, state=state, charged=0, refunded=refund, reason=reason,
        )

    def _refund(self, account: str, amount: int) -> None:
        if amount <= 0:
            return
        if self._redis is not None:
            try:
                self._redis.incrby(self._rkey("bal", account), amount)
                return
            except Exception as exc:
                _logger.warning("Redis 退款失败，降级进程内: %s", exc)
        with self._lock:
            self._balances[account] = self._balances.get(account, 0) + amount

    # ----------------------------------------------------- idempotency ----
    def is_duplicate(self, request_id: str) -> bool:
        if not request_id:
            return False
        if self._redis is not None:
            try:
                # SETNX：占位成功 = 首次；失败 = 重复
                return not bool(self._redis.set(self._rkey("idem", _idem_key(request_id)), "1",
                                                nx=True, ex=int(DEFAULT_HOLD_TTL_S)))
            except Exception as exc:
                _logger.warning("Redis 幂等检查失败，降级进程内: %s", exc)
        with self._lock:
            return request_id in self._settled_ids

    def _mark_settled(self, request_id: str) -> None:
        with self._lock:
            self._settled_ids.add(request_id)
            if len(self._settled_ids) > 10000:   # 防无界增长
                self._settled_ids = set(list(self._settled_ids)[-5000:])

    # --------------------------------------------------- housekeeping ----
    def sweep_expired(self, now: float | None = None) -> list[SettlementResult]:
        """兜底：进程崩溃导致悬挂的冻结，超时后全额退回。"""
        now = now if now is not None else time.time()
        out: list[SettlementResult] = []
        with self._lock:
            stale = [h for h in self._holds.values()
                     if h.state == HoldState.held and h.expires_at <= now]
            for h in stale:
                out.append(self._release_locked(h, HoldState.expired, "冻结超时，兜底全额退回"))
        return out

    def reset(self) -> None:
        """测试用：清空全部内存状态。"""
        with self._lock:
            self._balances.clear()
            self._holds.clear()
            self._settled_ids.clear()


_ledger: BillingLedger | None = None
_ledger_lock = threading.Lock()


def get_ledger() -> BillingLedger:
    global _ledger
    if _ledger is None:
        with _ledger_lock:
            if _ledger is None:
                _ledger = BillingLedger()
    return _ledger


def reset_ledger() -> None:
    """测试 / 配置热更新用。"""
    global _ledger
    with _ledger_lock:
        _ledger = None


def account_key(user_id: str | None, ip: str = "") -> str:
    """账户标识：登录用户按 id，匿名按 IP（与限流 key 口径保持一致）。"""
    return f"u:{user_id}" if user_id else f"ip:{ip or 'unknown'}"
