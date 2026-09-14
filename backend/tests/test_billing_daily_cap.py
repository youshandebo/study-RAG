# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""日消耗硬熔断（Daily Hard Cap）单元测试。

运行：cd backend && python -m pytest tests/test_billing_daily_cap.py -q

覆盖的不是"能不能加减数"，而是三个真正会被打穿的地方：
1. **并发**：限额必须在冻结那一刻生效，而不是等结算——否则脚本靠"同时在途"
   就能把检查架空；
2. **降级**：Redis 挂了必须继续在进程内冻结，而不是放行（放行 = 白嫖）；
3. **跨日**：计数第二天归零，且不把昨天的账带到今天。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.core.billing import (
    BillingLedger,
    DailyCapExceeded,
    HoldState,
    seconds_to_midnight,
)


def _ledger() -> BillingLedger:
    led = BillingLedger()
    led.reset()
    return led


class BoomRedis:
    """任何调用都抛错的假 Redis：用来验证降级路径不会变成"免费放行"。"""

    def __getattr__(self, name):  # noqa: D105 - 测试替身
        def _boom(*args, **kwargs):
            raise RuntimeError("redis down")

        return _boom


# ------------------------------------------------------- 计数口径 ----
class TestDailyAccounting:
    def test_freeze_counts_hold_then_settle_keeps_only_charge(self):
        led = _ledger()
        led.grant("u:1", 100_000)

        hold = led.reserve("u:1", 4000, daily_cap=10_000)
        # 冻结阶段：在途按上限占用，这样并发才不会超支
        assert led.daily_spent("u:1") == 4000

        led.settle(hold, effective_tokens=1500)
        # 结算后：只留下真实产出，虚高的冻结差额退回
        assert led.daily_spent("u:1") == 1500

    def test_release_gives_quota_back(self):
        led = _ledger()
        led.grant("u:1", 100_000)

        hold = led.reserve("u:1", 4000, daily_cap=10_000)
        led.release(hold, reason="零产出")
        assert led.daily_spent("u:1") == 0

    def test_low_yield_still_counts_as_no_charge(self):
        """低于 DEFAULT_MIN_BILLABLE_TOKENS 视为未交付：日配额也必须一并清零。"""
        led = _ledger()
        led.grant("u:1", 100_000)

        hold = led.reserve("u:1", 4000, daily_cap=10_000)
        res = led.settle(hold, effective_tokens=20)
        assert res.charged == 0 and led.daily_spent("u:1") == 0

    def test_compare_tracks_consume_n_times_quota(self):
        """分屏比对 N 条轨道 = N 倍成本，日配额必须同步放大。"""
        led = _ledger()
        led.grant("u:1", 1_000_000)

        led.reserve("u:1", 4000, units=3, daily_cap=12_000)
        assert led.daily_spent("u:1") == 12_000

        with pytest.raises(DailyCapExceeded):
            led.reserve("u:1", 4000, units=3, daily_cap=12_000)


# ------------------------------------------------------- 硬熔断 ----
class TestCapEnforcement:
    def test_exceeding_cap_raises_with_retry_after(self):
        led = _ledger()
        led.grant("u:1", 1_000_000)

        led.reserve("u:1", 4000, daily_cap=5_000)
        with pytest.raises(DailyCapExceeded) as ei:
            led.reserve("u:1", 4000, daily_cap=5_000)

        assert ei.value.retry_after_s > 0
        # 被拒绝的请求不能留下任何痕迹，否则重试会自我惩罚
        assert led.daily_spent("u:1") == 4000

    def test_exact_fit_allowed(self):
        led = _ledger()
        led.grant("u:1", 1_000_000)

        assert led.reserve("u:1", 4000, daily_cap=8_000) is not None
        assert led.reserve("u:1", 4000, daily_cap=8_000) is not None
        with pytest.raises(DailyCapExceeded):
            led.reserve("u:1", 4000, daily_cap=8_000)

    def test_cap_disabled_when_zero(self):
        led = _ledger()
        led.grant("u:1", 1_000_000)

        for _ in range(20):
            assert led.reserve("u:1", 4000, daily_cap=0) is not None
        assert led.daily_spent("u:1") == 0   # 不启用就不计数，省得白白占内存

    def test_cap_is_checked_before_balance(self):
        """两个条件同时成立时优先报日上限——让用户充值是误导，明天才能恢复。"""
        led = _ledger()
        led.grant("u:1", 4_000)

        assert led.reserve("u:1", 4000, daily_cap=4_000) is not None
        assert led.balance("u:1") == 0
        with pytest.raises(DailyCapExceeded):
            led.reserve("u:1", 4000, daily_cap=4_000)

    def test_insufficient_balance_still_returns_none(self):
        led = _ledger()
        led.grant("u:1", 1_000)
        assert led.reserve("u:1", 4000, daily_cap=1_000_000) is None

    def test_idempotent_replay_costs_nothing(self):
        led = _ledger()
        led.grant("u:1", 100_000)

        h1 = led.reserve("u:1", 4000, request_id="req-1", daily_cap=100_000)
        led.settle(h1, effective_tokens=1000, request_id="req-1")
        spent = led.daily_spent("u:1")

        h2 = led.reserve("u:1", 4000, request_id="req-1", daily_cap=100_000)
        assert h2 is not None and h2.state == HoldState.settled and h2.amount == 0
        assert led.daily_spent("u:1") == spent

    def test_expired_hold_refunds_quota(self):
        led = _ledger()
        led.grant("u:1", 100_000)

        led.reserve("u:1", 4000, daily_cap=100_000, ttl_s=0.01)
        time.sleep(0.02)
        assert led.sweep_expired()
        assert led.daily_spent("u:1") == 0


# ------------------------------------------------------- 跨日重置 ----
class TestDayRollover:
    def test_new_day_starts_from_zero(self):
        led = _ledger()
        led.grant("u:1", 100_000)
        led._today_fn = lambda: "20260914"

        hold = led.reserve("u:1", 4000, daily_cap=5_000)
        assert led.daily_spent("u:1") == 4000

        led._today_fn = lambda: "20260915"
        assert led.daily_spent("u:1") == 0
        assert led.daily_spent("u:1", "20260914") == 4000
        # 同一账户在新的一天可以继续使用
        assert led.reserve("u:1", 4000, daily_cap=5_000) is not None

        # 昨天的冻结结算回收到昨天那本账上，不污染今天
        led.settle(hold, effective_tokens=1000)
        assert led.daily_spent("u:1", "20260914") == 1000
        assert led.daily_spent("u:1") == 4000


# ------------------------------------------------------- 降级路径 ----
class TestDegradation:
    def test_redis_failure_still_freezes_locally(self):
        """Redis 抖动时不能退化成"不冻结"——那等于无限白嫖。"""
        led = _ledger()
        led._redis = BoomRedis()

        led.grant("u:1", 10_000)
        hold = led.reserve("u:1", 4000, daily_cap=5_000)
        assert hold is not None
        # 进程内确实冻住了 4000（旧实现这一步会被跳过）
        assert led.balance("u:1") == 6_000
        assert led.daily_spent("u:1") == 4000

    def test_redis_failure_still_enforces_cap(self):
        led = _ledger()
        led._redis = BoomRedis()
        led.grant("u:1", 1_000_000)

        led.reserve("u:1", 4000, daily_cap=5_000)
        with pytest.raises(DailyCapExceeded):
            led.reserve("u:1", 4000, daily_cap=5_000)


# ------------------------------------------------------- 辅助能力 ----
class TestHelpers:
    def test_seconds_to_midnight_in_range(self):
        assert 0 < seconds_to_midnight() <= 86400

    def test_daily_remaining(self):
        led = _ledger()
        led.grant("u:1", 1_000_000)
        assert led.daily_remaining("u:1", 0) == -1          # -1 = 无上限

        led.reserve("u:1", 4000, daily_cap=10_000)
        assert led.daily_remaining("u:1", 10_000) == 6_000

    def test_bucket_isolates_accounts(self):
        led = _ledger()
        led.grant("u:1", 1_000_000)
        ledger_shared = _ledger()
        ledger_shared.grant("u:1", 1_000_000)

        # 多租户模式下按租户计账：不同租户各有一本账，互不影响
        ledger_shared.reserve("u:1", 4000, daily_cap=5_000, daily_bucket="t:a")
        assert ledger_shared.daily_spent("u:1") == 0
        assert ledger_shared.daily_spent("t:a") == 4_000
        with pytest.raises(DailyCapExceeded):
            ledger_shared.reserve("u:1", 4000, daily_cap=5_000, daily_bucket="t:a")


# ------------------------------------------------------- 档位默认值 ----
class TestPlanDefaults:
    def test_defaults_by_tier(self):
        from app.core.membership import daily_cap_for

        assert daily_cap_for("free") == 120_000
        assert daily_cap_for("pro") == 600_000
        assert daily_cap_for("max") == 2_000_000
        assert daily_cap_for("guest") == 20_000

    def test_unknown_tier_falls_back_to_free(self):
        from app.core.membership import daily_cap_for

        assert daily_cap_for(None) == 120_000
        assert daily_cap_for("vip-未知档") == 120_000

    def test_runtime_config_can_override(self, monkeypatch):
        import app.core.runtime_config as rc
        from app.core.membership import daily_cap_for

        monkeypatch.setattr(
            rc, "effective",
            lambda key, default=None: {"free": {"daily_cap": 777}} if key == "plans" else default,
        )
        assert daily_cap_for("free") == 777

    def test_zero_means_unlimited(self, monkeypatch):
        import app.core.runtime_config as rc
        from app.core.membership import daily_cap_for

        monkeypatch.setattr(
            rc, "effective",
            lambda key, default=None: {"pro": {"daily_cap": 0}} if key == "plans" else default,
        )
        assert daily_cap_for("pro") == 0
