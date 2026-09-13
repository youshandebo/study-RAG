# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""测试套件公共隔离：把 runtime_config 重定向到临时文件。

为什么必须这么做
----------------
`runtime_config.py` 用模块级常量 `_CONFIG_PATH` 指向
`backend/data/runtime_config.json`——那是**开发者本机真实配置**。
如果测试直接读它，任何本地配置（比如为标定写入的 rerank.enabled=true）
都会泄漏进断言，让"默认值为空"这类测试在本机莫名失败、在 CI 却通过。

反过来，测试里调用 `save_runtime_config()` 也会**污染**开发者的真实配置。

会话级 autouse fixture 把 `_CONFIG_PATH` 重定向到临时目录：
- 断言默认值时拿到的是干净的空配置
- 测试写入配置只影响临时文件，不碰本机真实配置
"""
from __future__ import annotations

import pytest

from app.core import runtime_config as rc


@pytest.fixture(autouse=True, scope="session")
def isolate_runtime_config(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("runtime_cfg") / "runtime_config.json"
    original_path = rc._CONFIG_PATH
    rc._CONFIG_PATH = tmp
    rc._cache = None
    rc._cache_mtime = -1.0
    yield
    rc._CONFIG_PATH = original_path
    rc._cache = None
    rc._cache_mtime = -1.0
