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

日消耗硬熔断（Daily Hard Cap）
------------------------------
余额不足只保护"账面上有没有钱"，保护不了"上游会不会被刷爆"——限速约束的是
**发起速度**，一旦密钥泄露，脚本匀速提问照样能在一天里烧掉无限的模型账单。

日配额是成本核算的最后一道闸门，与预冻结共享同一套冻/退役语义：

    日计数 = 当日已扣减 + 当前在途冻结

- **预冻结时原子占用**（`reserve` 内的 Lua / 进程内锁）：把"在途"也算进计数，
  否则攻击者并发开 N 条 SSE，每条都在排队等结算，检查形同虚设。
- **结算时回退差额**：按实际用量结算后只留下 `charged`；全额退回则清零。
  因此计数最终收敛到真实消耗，而不是被冻结上限虚高。
- **跨日自动失效**：key 带当日 UTC+8 零点后的 TTL，次日自然归零，无需定时任务。

Redis 可用时上述动作由 Lua 脚本保证原子性；不可用时退回进程内锁——
口径与 `SlidingWindowLimiter` 一致：单副本下行为完全正确，多副本需配 Redis。
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum

_logger = logging.getLogger("app.core.billing")

# 低于此 token 数视为"未成功交付"，全额退回
DEFAULT_MIN_BILLABLE_TOKENS = 50
# 冻结凭证有效期：超过则视为悬挂，由 sweep_expired 退回
DEFAULT_HOLD_TTL_S = 300.0

# 日消耗计数的时间边界。按 UTC+8 切日：面向国内用户的 SaaS，"次日 00:00 重置"
# 指的是用户所在时区，而不是服务器时区——显式声明避免部署到 UTC 机器后语义漂移。
_UTC8 = timezone(timedelta(hours=8))
# 进程内日计数上限，超出后清掉非当日条目，防字典无界增长
_MAX_DAILY_ENTRIES = 5000

# reserve 冻结的状态码（Lua 与 Python 两侧共用，必须保持一致）
_RESERVE_OK = 1
_RESERVE_CAP_BLOCKED = 2
_RESERVE_NO_BALANCE = 3

# 「冻结余额 + 占用日配额」必须是一次原子动作：分成两步的话，
# 高并发下两个请求可能同时通过日配额检查，再各自冻住余额——限额被击穿。
# Lua 脚本在 Redis 单线程里一次跑完，天然排除这个窗口。
_RESERVE_LUA = """
local total = tonumber(ARGV[1])
local cap = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
local spent = 0
if cap > 0 then
  spent = tonumber(redis.call('GET', KEYS[2]) or '0')
  if spent + total > cap then
    return {%d, spent}
  end
end
local bal = tonumber(redis.call('DECRBY', KEYS[1], total))
if bal < 0 then
  redis.call('INCRBY', KEYS[1], total)
  return {%d, spent}
end
if cap > 0 then
  spent = tonumber(redis.call('INCRBY', KEYS[2], total))
  if redis.call('TTL', KEYS[2]) < 0 then
    redis.call('EXPIRE', KEYS[2], ttl)
  end
end
return {%d, spent}
""" % (_RESERVE_CAP_BLOCKED, _RESERVE_NO_BALANCE, _RESERVE_OK)

# 日计数回退：只在 key 还在时才扣，且夹紧到 0。
# 「key 不在」说明已跨日过期——此时扣减会把已失效的 key 复活成负数。
_DAILY_DECR_LUA = """
if redis.call('EXISTS', KEYS[1]) == 1 then
  local v = tonumber(redis.call('DECRBY', KEYS[1], ARGV[1]))
  if v < 0 then
    redis.call('INCRBY', KEYS[1], 0 - v)
    return 0
  end
  return v
end
return 0
"""


def utc8_today() -> str:
    """当日 UTC+8 日期戳（YYYYMMDD），日计数的分桶键。"""
    return datetime.now(_UTC8).strftime("%Y%m%d")


def seconds_to_midnight(now: datetime | None = None) -> int:
    """距离下一个 UTC+8 零点的秒数，用作 429 的 Retry-After。"""
    now = now or datetime.now(_UTC8)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((midnight - now).total_seconds()))


class DailyCapExceeded(Exception):
    """当日消耗（含在途冻结）触达硬上限。调用方应回 429 + Retry-After。"""

    def __init__(self, cap: int, spent: int, projected: int, retry_after_s: int) -> None:
        self.cap = cap
        self.spent = spent
        self.projected = projected
        self.retry_after_s = retry_after_s
        super().__init__(
            f"日消耗触顶：已计 {spent} + 本次 {projected} > 上限 {cap}，"
            f"{retry_after_s} 秒后重置"
        )


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
    daily_date: str = ""           # 非空 = 本次冻结已计入该日的日配额，退役时需回退
    daily_bucket: str = ""         # 日配额归属账户（默认 = account，多租户下为租户 id）

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
        # 日配额计数：key = f"{date}:{bucket}"；同上，配置 Redis 时以 Redis 为准
        self._daily: dict[str, int] = {}
        self._reserve_lua = None      # 懒加载，避免无 Redis 时也去建 Script 对象
        self._decr_lua = None
        # 可注入的时钟钩子：生产走真实时间，测试用它模拟跨日重置
        self._today_fn = utc8_today
        self._ttl_fn = seconds_to_midnight

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

    def _daily_rkey(self, bucket: str, date: str) -> str:
        return self._rkey("daily", date, bucket)

    def _get_reserve_script(self):
        if self._reserve_lua is None:
            self._reserve_lua = self._redis.register_script(_RESERVE_LUA)
        return self._reserve_lua

    def _get_decr_script(self):
        if self._decr_lua is None:
            self._decr_lua = self._redis.register_script(_DAILY_DECR_LUA)
        return self._decr_lua

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

    def debit(self, account: str, amount: int) -> int:
        """直接扣减（管理后台人工调减 / 纠错用），夹紧到 0 不允许负余额。

        与 `reserve/settle` 的区别：这条路径**没有冻结凭证**，是运营侧的
        账务动作，不是一次消费。走同一套原子操作（Redis `DECRBY` / 进程内锁），
        保证与并发的冻结-结算不会互相覆盖。
        """
        amount = max(0, int(amount))
        if amount <= 0:
            return self.balance(account)
        if self._redis is not None:
            try:
                left = int(self._redis.decrby(self._rkey("bal", account), amount))
                if left < 0:
                    self._redis.incrby(self._rkey("bal", account), -left)  # 回补到 0
                    return 0
                return left
            except Exception as exc:
                _logger.warning("Redis 扣减失败，降级进程内: %s", exc)
        with self._lock:
            left = max(0, self._balances.get(account, 0) - amount)
            self._balances[account] = left
            return left

    # ---------------------------------------------------------- hold ----
    def reserve(
        self,
        account: str,
        amount: int,
        *,
        request_id: str = "",
        units: int = 1,
        ttl_s: float = DEFAULT_HOLD_TTL_S,
        daily_cap: int = 0,
        daily_bucket: str = "",
    ) -> Hold | None:
        """预冻结。余额不足返回 None（调用方应回 402）；触达日上限抛出 `DailyCapExceeded`。

        `units` 是**计费单位数**——compare 并发 N 条模型轨道就传 N，
        让"多倍成本"在冻结阶段就体现为多倍占用，而非结算时才被发现。
        `amount` 是**单位额度**，最终冻结 amount * units。

        `daily_cap`：> 0 时校验当日消耗硬上限（取值见 `membership.daily_cap_for`）。
        `daily_bucket`：日计数的归属账户，默认按 `account`（用户 / 匿名 IP）。
        多租户模式下应传**租户 id**，让整个机构共享一份日预算——这才是
        "租户级熔断"。单租户部署不能传，否则所有用户会互相挤占同一份配额。
        """
        amount = max(0, int(amount))
        units = max(1, int(units))
        total = amount * units

        cap = max(0, int(daily_cap or 0))
        bucket = daily_bucket or account
        date = self._today_fn() if cap > 0 else ""

        if request_id and self.is_duplicate(request_id):
            # 重复请求：不重复冻结，返回一个零额度凭证供上层走幂等返回
            h = Hold(hold_id=f"dup-{request_id}", account=account, amount=0, units=units)
            h.state = HoldState.settled
            h.charged = 0
            return h

        spent, ok = self._commit_freeze(account, bucket, total, cap, date)
        if ok is None:
            return None                       # 余额不足 → 上层回 402
        if ok is False:
            raise DailyCapExceeded(cap=cap, spent=spent, projected=total,
                                   retry_after_s=self._ttl_fn())

        hold = Hold(hold_id=uuid.uuid4().hex[:16], account=account, amount=total, units=units)
        hold.expires_at = hold.created_at + float(ttl_s or DEFAULT_HOLD_TTL_S)
        hold.daily_date = date
        hold.daily_bucket = bucket
        with self._lock:
            self._holds[hold.hold_id] = hold
        return hold

    def _commit_freeze(
        self, account: str, bucket: str, total: int, cap: int, date: str,
    ) -> tuple[int, bool | None]:
        """把「冻结余额」与「占用日配额」做成一次原子动作。

        返回 `(spent_after, status)`：True = 已冻结；False = 触达日上限；None = 余额不足。
        """
        if self._redis is not None:
            try:
                code, spent = self._get_reserve_script()(
                    keys=[self._rkey("bal", account), self._daily_rkey(bucket, date)],
                    # TTL 多留 60 秒兜底时钟漂移，避免零点前后跨 key 时漏数
                    args=[total, cap, self._ttl_fn() + 60],
                )
                code, spent = int(code), int(spent)
                if code == _RESERVE_OK:
                    return spent, True
                if code == _RESERVE_CAP_BLOCKED:
                    return spent, False
                return spent, None
            except Exception as exc:
                # 降级必须**继续走进程内冻结**，而不是直接放行——
                # 放行等于"Redis 一抖就能无限白嫖"。
                _logger.warning("Redis 冻结失败，降级进程内: %s", exc)
        with self._lock:
            spent = self._daily.get(f"{date}:{bucket}", 0) if cap > 0 else 0
            if cap > 0 and spent + total > cap:
                return spent, False
            if self._balances.get(account, 0) < total:
                return spent, None
            self._balances[account] = self._balances.get(account, 0) - total
            if cap > 0:
                self._daily[f"{date}:{bucket}"] = spent + total
                self._prune_daily_locked(date)
                return spent + total, True
            return 0, True

    # ------------------------------------------------------ daily cap ----
    def daily_spent(self, bucket: str, date: str | None = None) -> int:
        """当日已累计消耗（当日已扣减 + 在途冻结），运营后台与自查用。"""
        date = date or self._today_fn()
        if self._redis is not None:
            try:
                raw = self._redis.get(self._daily_rkey(bucket, date))
                return int(raw) if raw is not None else 0
            except Exception as exc:
                _logger.warning("Redis 日计数读取失败，降级进程内: %s", exc)
        with self._lock:
            return self._daily.get(f"{date}:{bucket}", 0)

    def daily_remaining(self, bucket: str, cap: int) -> int:
        """当日剩余可用额度；`cap <= 0`（不限）时返回 -1 表示无上限。"""
        if cap <= 0:
            return -1
        return max(0, cap - self.daily_spent(bucket))

    def _daily_unwind(self, hold: Hold, charged_final: int) -> None:
        """冻结退役时把未真正扣减的部分从日计数里退出去。

        冻结阶段计入了 `hold.amount`，退役后只应留下 `charged_final`：
        部分结算 → 回退差额；全额退回 → 全部回退。计数因此收敛到真实消耗。
        """
        if not hold.daily_date:
            return
        release = hold.amount - max(0, int(charged_final or 0))
        if release <= 0:
            return
        date = hold.daily_date
        bucket = hold.daily_bucket or hold.account
        if self._redis is not None:
            try:
                self._get_decr_script()(keys=[self._daily_rkey(bucket, date)], args=[release])
                return
            except Exception as exc:
                _logger.warning("Redis 日计数回退失败，降级进程内: %s", exc)
        with self._lock:
            k = f"{date}:{bucket}"
            if k in self._daily:
                self._daily[k] = max(0, self._daily[k] - release)
                if self._daily[k] == 0:
                    self._daily.pop(k, None)

    def _prune_daily_locked(self, today: str) -> None:
        """只保留当日条目：昨天的 key 本就该随 TTL 失效，留在字典里只会泄漏内存。"""
        if len(self._daily) <= _MAX_DAILY_ENTRIES:
            return
        for k in [k for k in self._daily if not k.startswith(f"{today}:")]:
            self._daily.pop(k, None)

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
        # 日配额同步退役：冻结时占了 amount，结算后只留 charge
        self._daily_unwind(current, charge)
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
        self._daily_unwind(hold, 0)   # 全额退回：日配额同步清零
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
            self._daily.clear()


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
