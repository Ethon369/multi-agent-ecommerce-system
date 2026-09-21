"""
工具层测试 —— 全部离线，用假工具，不起子进程。

    这一层测的是「保障」而不是「业务」
    ────────────────────────────────
    工具本身干什么（查库存、查指标）由各自的测试覆盖。
    这里要守住的是【每个工具都被同样的保障包裹】：超时 / 重试 / 降级 /
    失败即值 / 观测标记。

    这些保障如果只写在某个工具里，加新工具时就会漏 —— 那正是这一层存在的理由。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from harness.tools.registry import ToolRegistry
from harness.tools.spec import ToolSpec, fn_to_async


async def _ok(**kwargs: Any) -> Any:
    return {"echo": kwargs}


def make_registry(**spec_kwargs: Any) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="echo",
            description="回声",
            input_schema={"type": "object", "properties": {}, "required": []},
            handler=_ok,
            **spec_kwargs,
        )
    )
    return reg


# ── 基本分发 ────────────────────────────────────────────────

@pytest.mark.anyio
async def test_call_returns_value() -> None:
    reg = make_registry()
    r = await reg.call("echo", a=1)
    assert r.ok is True
    assert r.value == {"echo": {"a": 1}}
    assert r.attempts == 1
    assert r.degraded is False


@pytest.mark.anyio
async def test_unknown_tool_is_a_value_not_an_exception() -> None:
    """
    模型会幻觉出不存在的工具名。

    这必须是【值】而不是 KeyError —— 否则一次幻觉就能把整条请求打挂。
    返回的错误信息里带上"有哪些可用"，让模型下一轮能自我纠正。
    """
    reg = make_registry()
    r = await reg.call("no_such_tool")

    assert r.ok is False
    assert r.error == "unknown_tool:no_such_tool"
    assert r.attempts == 0


def test_duplicate_name_rejected() -> None:
    """重名会让分发变得不可预测，必须在注册时就拦住。"""
    reg = make_registry()
    with pytest.raises(ValueError, match="重复"):
        reg.register(
            ToolSpec(name="echo", description="x", input_schema={}, handler=_ok)
        )


# ── 超时 ────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_timeout_is_enforced() -> None:
    async def slow(**kwargs: Any) -> Any:
        await asyncio.sleep(5)

    reg = ToolRegistry()
    reg.register(ToolSpec(
        name="slow", description="慢", input_schema={},
        handler=slow, timeout_s=0.05,
    ))

    t0 = asyncio.get_event_loop().time()
    r = await reg.call("slow")
    elapsed = asyncio.get_event_loop().time() - t0

    assert elapsed < 1.0, f"超时没生效，等了 {elapsed:.2f}s"
    assert r.ok is False
    assert "TimeoutError" in (r.error or "")


# ── 重试 ────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_no_retry_by_default() -> None:
    """
    默认不重试。

    理由：一个超时 3 秒的只读工具重试两次，会在一条本来就 16 秒的链路上
    再烧 6 秒，换来的还是同一个答案。重试是【按工具逐个决定】的。
    """
    calls = {"n": 0}

    async def flaky(**kwargs: Any) -> Any:
        calls["n"] += 1
        raise ConnectionError("boom")

    reg = ToolRegistry()
    reg.register(ToolSpec(
        name="flaky", description="x", input_schema={}, handler=flaky, timeout_s=1.0
    ))

    r = await reg.call("flaky")
    assert calls["n"] == 1, f"默认不该重试，实际调了 {calls['n']} 次"
    assert r.attempts == 1


@pytest.mark.anyio
async def test_retries_transient_errors_then_succeeds() -> None:
    calls = {"n": 0}

    async def flaky(**kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError(f"boom-{calls['n']}")
        return "finally"

    reg = ToolRegistry()
    reg.register(ToolSpec(
        name="flaky", description="x", input_schema={},
        handler=flaky, timeout_s=1.0, max_attempts=3,
    ))

    r = await reg.call("flaky")
    assert r.ok is True
    assert r.value == "finally"
    assert r.attempts == 3
    assert calls["n"] == 3


@pytest.mark.anyio
async def test_business_errors_are_not_retried() -> None:
    """
    只重试瞬时故障（超时/连接）。

    业务异常（比如参数不合法）重试多少次都是同一个结果，只会白花时间。
    """
    calls = {"n": 0}

    async def bad(**kwargs: Any) -> Any:
        calls["n"] += 1
        raise ValueError("bad argument")

    reg = ToolRegistry()
    reg.register(ToolSpec(
        name="bad", description="x", input_schema={},
        handler=bad, timeout_s=1.0, max_attempts=5,
    ))

    r = await reg.call("bad")
    assert calls["n"] == 1, f"业务异常被重试了 {calls['n']} 次"
    assert r.ok is False


# ── 降级 ────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_fallback_produces_degraded_success() -> None:
    """
    ok=True 与 degraded=True 必须能同时成立。

    如果只有 ok 一个字段，"用旧数据顶上了"就没法表达 ——
    要么谎报成功、要么谎报失败，两个都不对。
    """
    async def broken(**kwargs: Any) -> Any:
        raise ConnectionError("down")

    reg = ToolRegistry()
    reg.register(ToolSpec(
        name="broken", description="x", input_schema={},
        handler=broken, timeout_s=1.0,
        fallback=lambda **kw: {"stale": True},
    ))

    r = await reg.call("broken")
    assert r.ok is True, "降级成功时调用方应当能继续"
    assert r.degraded is True, "但必须标记这是次优结果"
    assert r.value == {"stale": True}
    assert r.error is not None, "原始错误要保留，否则排查时看不到真相"


@pytest.mark.anyio
async def test_fallback_failure_reports_original_error() -> None:
    async def broken(**kwargs: Any) -> Any:
        raise ConnectionError("down")

    def bad_fallback(**kwargs: Any) -> Any:
        raise RuntimeError("fallback also broken")

    reg = ToolRegistry()
    reg.register(ToolSpec(
        name="broken", description="x", input_schema={},
        handler=broken, timeout_s=1.0, fallback=bad_fallback,
    ))

    r = await reg.call("broken")
    assert r.ok is False
    assert "ConnectionError" in (r.error or ""), "应当报【原始】错误"


# ── 标签与 schema ───────────────────────────────────────────

def test_tag_filtering() -> None:
    reg = ToolRegistry()
    reg.register(ToolSpec(
        name="read", description="", input_schema={}, handler=_ok,
        tags=frozenset({"read_only"}),
    ))
    reg.register(ToolSpec(
        name="write", description="", input_schema={}, handler=_ok,
        tags=frozenset({"write"}),
    ))

    assert reg.names({"read_only"}) == ["read"]
    assert reg.names(None) == ["read", "write"]
    assert [s["name"] for s in reg.openai_schemas({"read_only"})] == ["read"]


def test_openai_schemas_uses_mcp_native_shape() -> None:
    """
    必须直接吐 MCP 的原生形状 {"name","description","input_schema"}，
    不做转换。

    依据（已实测）：langchain-core 的 convert_to_openai_function 有专门分支
    处理这个形状，bind_tools 内部会调它。这也正是
    "不需要 langchain-mcp-adapters"的技术依据。
    """
    reg = make_registry()
    schemas = reg.openai_schemas()

    assert len(schemas) == 1
    s = schemas[0]
    assert set(s.keys()) == {"name", "description", "input_schema"}
    assert s["input_schema"]["type"] == "object", "JSON Schema 必须原样透出"


def test_snapshot_is_serialisable() -> None:
    import json

    reg = make_registry()
    json.dumps(reg.snapshot())


# ── 同步函数包装 ────────────────────────────────────────────

@pytest.mark.anyio
async def test_sync_handler_is_wrapped() -> None:
    def sync_tool(**kwargs: Any) -> str:
        return "sync-ok"

    reg = ToolRegistry()
    reg.register(ToolSpec(
        name="sync", description="", input_schema={},
        handler=fn_to_async(sync_tool),
    ))

    r = await reg.call("sync")
    assert r.ok is True
    assert r.value == "sync-ok"


# ── MCP 来源：写操作识别（离线，不连 Server）────────────────

@pytest.mark.parametrize("name,expected", [
    ("query_stock", True),
    ("batch_query_stock", True),
    ("list_low_stock", True),
    ("get_metrics", True),
    ("upsert_stock", False),
    ("delete_product", False),
    ("update_stock", False),
])
def test_write_tool_detection(name: str, expected: bool) -> None:
    """
    写操作识别的【保守性】测试。

    漏判的后果是"写操作被暴露给了模型"，所以拿不准的一律当写操作。
    """
    from harness.tools.mcp_source import _is_read_only

    assert _is_read_only(name) is expected
