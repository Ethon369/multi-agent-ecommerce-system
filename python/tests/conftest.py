"""
pytest 公共装置。

1. 消除 sys.path 样板 —— 原先每个测试文件各自复制一遍
   `sys.path.insert(...)`（见 tests/test_ab_test.py 顶部）。
2. 提供把日志捕进列表的 `captured` 装置。

注意：不需要 pytest-asyncio。anyio 自带 pytest 插件（实测 anyio 4.15.1
会出现在 `pytest --version` 的 plugins 列表里），异步测试用
`@pytest.mark.anyio` 即可。
"""

from __future__ import annotations

import os
import sys
from typing import Any

import pytest
import structlog
from structlog.contextvars import merge_contextvars

PYTHON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PYTHON_DIR not in sys.path:
    sys.path.insert(0, PYTHON_DIR)


@pytest.fixture
def anyio_backend() -> str:
    """把 anyio 钉在 asyncio 上，避免它去跑 trio。"""
    return "asyncio"


@pytest.fixture
def captured() -> list[dict[str, Any]]:
    """
    把日志捕到列表里，且【保留 merge_contextvars 在最前面】。

    不直接用 structlog.testing.capture_logs()，因为它会整体替换 processor 链，
    把 merge_contextvars 一并拿掉 —— 那样测的就不是真实链路了。
    """
    events: list[dict[str, Any]] = []

    def sink(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        events.append(dict(event_dict))
        raise structlog.DropEvent

    # 测试隔离：pytest 默认所有测试跑在同一个 context 里，而
    # structlog.contextvars.bind_contextvars 是【永久性】的 —— 它不像
    # bound_contextvars 那样在退出时还原。结果就是一个测试里 bind 的
    # agent/attempt 会漏进后面的测试，让断言看到"上一个测试的残留值"。
    # 实测踩到过：泄漏出来的 agent='product_rec' 根本不是当前测试绑的。
    structlog.contextvars.clear_contextvars()
    structlog.configure(
        processors=[merge_contextvars, sink],
        cache_logger_on_first_use=False,
    )
    try:
        yield events
    finally:
        structlog.contextvars.clear_contextvars()
        structlog.reset_defaults()
