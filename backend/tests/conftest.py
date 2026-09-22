"""
pytest 公共装置。

1. 消除 sys.path 样板 —— 原先每个测试文件各自复制一遍
   `sys.path.insert(...)`。
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

# ── 测试环境的配置默认值 ────────────────────────────────────────
#
# ⚠️ 必须在【任何测试模块导入 main 之前】设好。
# 原因：main.py 在【导入期】就调 configure_logging(settings.log_json, ...)，
# 而 get_settings() 带 @lru_cache —— 设晚了就再也不会重读。
# tests/test_api.py 按字母序是第一个被导入的测试模块，所以这几行
# 放在 conftest 的模块级（conftest 一定先于所有测试模块加载）。

# 控制台格式而不是 JSON：pytest 的失败输出里，一行 JSON 几乎没法读，
# 而且它会把 assert 的上下文淹没掉。
os.environ.setdefault("ECOM_LOG_JSON", "false")

# 把级别放回 DEBUG。configure_logging 用 make_filtering_bound_logger
# 设了一个【全局】的过滤级别，生产上要 INFO；但生产代码里有 3 处
# logger.debug（tool.registered / mcp.tool_skipped_write / harness/llm.py），
# 级别卡在 INFO 会让它们在测试里彻底消失 —— 那类"这个 debug 事件到底
# 有没有发"的问题就没法回答了。测试里恢复成"什么都不过滤"。
os.environ.setdefault("ECOM_LOG_LEVEL", "DEBUG")

# 鉴权默认必须是关的：接口级测试直接打路由，不该被迫带 key。
# 这一条同时也是断言的一部分（见 test_api.py 的默认配置用例）——
# 它守的是"新能力默认不启用"这条项目约定。
os.environ.setdefault("ECOM_API_KEY_ENABLED", "false")


@pytest.fixture
def anyio_backend() -> str:
    """把 anyio 钉在 asyncio 上，避免它去跑 trio。"""
    return "asyncio"


@pytest.fixture(autouse=True)
def _isolate_agent_runtime():
    """
    每个测试前后丢弃 AgentRuntime 单例。

    AgentRuntime 是进程级的，熔断状态会跨测试累积 —— 一个测试打出的
    熔断会让后面所有测试拿到 circuit_open，结果不可复现。
    本项目已经在 structlog contextvars 上踩过一次同类问题，
    所以这里直接用 autouse 兜住，不指望每个测试自己记得清。
    """
    from harness import reset_runtime

    reset_runtime()
    try:
        yield
    finally:
        reset_runtime()


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


# ── 假 Redis ────────────────────────────────────────────────────
#
# 项目测外部依赖一律【手写假对象】，不引入 mock 库
# （见 tests/test_inventory_mcp.py 的 FakeMCPClient）。
# requirements.txt 里也没有 fakeredis —— 为几个用例加一个依赖不划算。
#
# 只实现 FeatureStore 真正用到的那几个命令。窄是有意的：假对象的面越小，
# 它和真实现的偏差就越小；一个"什么都支持"的假 Redis 反而会给出
# 真实环境里不成立的结论。


def _score_in_range(score: float, low: Any, high: Any) -> bool:
    if low != "-inf" and score < low:
        return False
    if high != "+inf" and score > high:
        return False
    return True


class FakeRedis:
    """只实现 FeatureStore 用到的命令的内存假 Redis。"""

    def __init__(self) -> None:
        self.zsets: dict[str, dict[str, float]] = {}
        self.hashes: dict[str, dict[str, Any]] = {}
        self.strings: dict[str, str] = {}
        self.expires: dict[str, int] = {}
        #: 设成某个命令名（如 "zcount"）后该命令会抛异常 —— 用来测降级。
        self.raise_on: str | None = None
        #: 记录发生过的命令名，用来断言"降级时没有继续往下打"。
        self.commands: list[str] = []

    def _maybe_raise(self, command: str) -> None:
        self.commands.append(command)
        if self.raise_on == command:
            raise ConnectionError(f"fake redis: {command} unavailable")

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        self._maybe_raise("zadd")
        zset = self.zsets.setdefault(key, {})
        added = sum(1 for member in mapping if member not in zset)
        zset.update(mapping)
        return added

    async def zcount(self, key: str, low: Any, high: Any) -> int:
        self._maybe_raise("zcount")
        return sum(
            1
            for score in self.zsets.get(key, {}).values()
            if _score_in_range(score, low, high)
        )

    async def zrangebyscore(
        self, key: str, low: Any, high: Any, withscores: bool = False
    ) -> list:
        self._maybe_raise("zrangebyscore")
        items = sorted(
            (
                (member, score)
                for member, score in self.zsets.get(key, {}).items()
                if _score_in_range(score, low, high)
            ),
            key=lambda kv: kv[1],
        )
        return items if withscores else [member for member, _ in items]

    async def zrevrangebyscore(
        self,
        key: str,
        high: Any,
        low: Any,
        start: int | None = None,
        num: int | None = None,
        withscores: bool = False,
    ) -> list:
        self._maybe_raise("zrevrangebyscore")
        items = sorted(
            (
                (member, score)
                for member, score in self.zsets.get(key, {}).items()
                if _score_in_range(score, low, high)
            ),
            key=lambda kv: kv[1],
            reverse=True,
        )
        if start is not None:
            items = items[start : None if num is None else start + num]
        return items if withscores else [member for member, _ in items]

    async def zremrangebyscore(self, key: str, low: Any, high: Any) -> int:
        self._maybe_raise("zremrangebyscore")
        zset = self.zsets.get(key)
        if not zset:
            return 0
        doomed = [m for m, s in zset.items() if _score_in_range(s, low, high)]
        for member in doomed:
            del zset[member]
        return len(doomed)

    async def expire(self, key: str, ttl: int) -> bool:
        self._maybe_raise("expire")
        self.expires[key] = ttl
        return True

    async def set(self, key: str, value: Any, ex: int | None = None) -> bool:
        self._maybe_raise("set")
        self.strings[key] = str(value)
        if ex is not None:
            self.expires[key] = ex
        return True

    async def get(self, key: str) -> str | None:
        self._maybe_raise("get")
        return self.strings.get(key)

    async def hset(self, key: str, field: str, value: Any) -> int:
        self._maybe_raise("hset")
        self.hashes.setdefault(key, {})[field] = value
        return 1

    async def hgetall(self, key: str) -> dict[str, Any]:
        self._maybe_raise("hgetall")
        return dict(self.hashes.get(key, {}))

    async def delete(self, *keys: str) -> int:
        self._maybe_raise("delete")
        removed = 0
        for key in keys:
            for store in (self.zsets, self.hashes, self.strings, self.expires):
                if key in store:
                    del store[key]
                    removed += 1
        return removed

    async def scan_iter(self, match: str = "*"):
        import fnmatch

        every = set(self.zsets) | set(self.hashes) | set(self.strings)
        for key in sorted(every):
            if fnmatch.fnmatch(key, match):
                yield key

    async def ping(self) -> bool:
        self._maybe_raise("ping")
        return True


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()
