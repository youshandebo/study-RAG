# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""启动探针测试：把"多副本 + 无共享状态"钉死成拒绝启动。

这里最核心的一条不变量是 `test_ack_does_not_override_multi_replica`：
逃生舱只允许单副本使用。若哪天有人"顺手"让 ACK 也能放过多副本，
配额放大与闸门失效就会以"部署方已确认"的名义重新变成静默故障。
"""
from __future__ import annotations

import pytest

from app.core import boot_probe

_ENV_KEYS = (
    "ENV", "APP_ENV", "ENVIRONMENT", "REDIS_URL",
    "UVICORN_WORKERS", "WEB_CONCURRENCY", "REPLICAS",
    "ALLOW_PROCESS_LOCAL_STATE",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """清空所有相关变量，每个用例从"最小可部署环境"出发自建前置条件。"""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    # 驱动可用性默认按"已安装"处理，避免测试机差异影响判定；
    # 需要验证缺驱动的用例自行覆盖。
    monkeypatch.setattr(boot_probe, "redis_driver_available", lambda: True)
    return monkeypatch


def test_multi_replica_without_redis_exits(clean_env):
    clean_env.setenv("UVICORN_WORKERS", "4")
    with pytest.raises(SystemExit) as exc:
        boot_probe.enforce_runtime_safety()
    message = str(exc.value)
    assert "多副本" in message
    assert "REDIS_URL" in message
    # 报错必须说清后果，否则运维只会看到一句"缺环境变量"
    assert "配额被放大" in message and "闸门" in message


def test_multi_replica_from_argv_exits(clean_env):
    """`uvicorn app.main:app --workers 4` 既没写 compose 也没设变量——最易漏的组合。"""
    with pytest.raises(SystemExit):
        boot_probe.enforce_runtime_safety(["--workers", "4"])


def test_multi_replica_from_argv_equals_form(clean_env):
    with pytest.raises(SystemExit):
        boot_probe.enforce_runtime_safety(["--workers=3"])


def test_replicas_hint_takes_max_of_sources(clean_env):
    clean_env.setenv("UVICORN_WORKERS", "2")
    clean_env.setenv("REPLICAS", "5")
    assert boot_probe.replicas_hint([]) == 5


def test_production_single_replica_without_redis_exits(clean_env):
    clean_env.setenv("ENV", "production")
    with pytest.raises(SystemExit) as exc:
        boot_probe.enforce_runtime_safety()
    assert "生产环境" in str(exc.value)


def test_production_with_ack_allows_and_flags(clean_env):
    """单副本生产：显式确认后放行，但摘要里必须留痕（可观测，不是"悄悄降级"）。"""
    clean_env.setenv("ENV", "production")
    clean_env.setenv("ALLOW_PROCESS_LOCAL_STATE", "1")
    state = boot_probe.enforce_runtime_safety()
    assert state["state_backend"] == "process"
    assert state["acknowledged"] is True
    assert state["strict"] is True


def test_ack_does_not_override_multi_replica(clean_env):
    """逃生舱对多副本无效——脑裂是数据错误，不是配置取舍。"""
    clean_env.setenv("UVICORN_WORKERS", "4")
    clean_env.setenv("ALLOW_PROCESS_LOCAL_STATE", "1")
    with pytest.raises(SystemExit):
        boot_probe.enforce_runtime_safety()


def test_redis_configured_but_driver_missing_is_treated_as_missing(clean_env):
    """配了地址但驱动缺失 = 三方组件各自 except 后静默回落，等同于没配。"""
    clean_env.setenv("ENV", "production")
    clean_env.setenv("REDIS_URL", "redis://cache:6379/0")
    clean_env.setattr(boot_probe, "redis_driver_available", lambda: False)
    with pytest.raises(SystemExit) as exc:
        boot_probe.enforce_runtime_safety()
    assert "驱动缺失" in str(exc.value)


def test_dev_single_replica_degrades_with_warning(clean_env):
    """开发环境单副本不阻断（否则本地跑不起来），但后端必须是 process 且可观测。"""
    state = boot_probe.enforce_runtime_safety()
    assert state["state_backend"] == "process"
    assert state["strict"] is False


def test_redis_ready_passes_in_production(clean_env):
    clean_env.setenv("ENV", "production")
    clean_env.setenv("REDIS_URL", "redis://cache:6379/0")
    state = boot_probe.enforce_runtime_safety()
    assert state["state_backend"] == "redis"
    assert state["redis_driver"] is True


def test_prod_also_covered_via_app_env_alias(clean_env):
    clean_env.setenv("APP_ENV", "prod")
    with pytest.raises(SystemExit):
        boot_probe.enforce_runtime_safety()


def test_health_exposes_state_backend():
    """共享状态后端必须能从 /health 读到——否则运维无法判断是否真的共享。"""
    from fastapi.testclient import TestClient

    from app.main import app

    resp = TestClient(app).get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert "state" in body
    assert body["state"]["backend"] in ("redis", "process")
    assert isinstance(body["state"]["replicas"], int)
