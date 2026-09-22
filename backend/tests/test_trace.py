"""
harness.trace 的测试。

重点不是"函数能跑"，而是验证 M1 的【核心主张】：
    bind 一次 request_id 之后，那些一行都没改过的 logger.info
    会自动带上它 —— 不需要碰 4 个 Agent 里的任何一行日志代码。

最后一条测试（并发传播）验证的是计划里标注的坑 3：
    asyncio 任务在创建时【复制】上下文，所以
      - gather 之前绑定的字段会传播给子任务 ✓
      - 子任务里绑定的字段不会泄漏给兄弟任务 ✓
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import structlog
from structlog.contextvars import merge_contextvars

from harness import (
    bind,
    current_request_id,
    new_request_id,
    request_context,
    scope,
)


def test_default_chain_includes_merge_contextvars() -> None:
    """
    验证 M1 的立论基础：structlog 的【默认】processor 链第一项就是
    merge_contextvars。若这条断言失败，整个"零改动加 request_id"的方案不成立。
    """
    structlog.reset_defaults()
    structlog.get_logger().info("trigger-lazy-default-config")
    processors = structlog.get_config()["processors"]
    assert merge_contextvars in processors, (
        f"默认链里没有 merge_contextvars，实际是: {processors}"
    )
    assert processors[0] is merge_contextvars, "它必须是第一项，否则合并发生得太晚"


def test_new_request_id_unique() -> None:
    ids = {new_request_id() for _ in range(50)}
    assert len(ids) == 50


def test_current_request_id_outside_context_is_none() -> None:
    assert current_request_id() is None


def test_request_context_exposes_id(captured: list[dict[str, Any]]) -> None:
    assert current_request_id() is None
    with request_context("rid-123", user_id="u1") as rid:
        assert rid == "rid-123"
        assert current_request_id() == "rid-123"
    assert current_request_id() is None, "退出上下文后必须复位"


def test_untouched_logger_picks_up_request_id(captured: list[dict[str, Any]]) -> None:
    """
    M1 的核心主张：一行都没改过的 logger.info 自动带上 request_id。
    这里刻意不调用 harness 的任何函数，模拟 base_agent.py 里现有的日志语句。
    """
    plain_logger = structlog.get_logger()  # 和 base_agent.py:12 的写法一模一样

    with request_context("rid-abc"):
        # 完全模拟 base_agent.py:37 的既有日志语句，一个字都没改
        plain_logger.info("agent.success", agent="user_profile", latency_ms=1234.5)

    assert len(captured) == 1
    ev = captured[0]
    assert ev["event"] == "agent.success"
    assert ev["agent"] == "user_profile"
    assert ev["request_id"] == "rid-abc", "request_id 没有自动注入 —— M1 的核心主张不成立"


def test_bind_adds_fields(captured: list[dict[str, Any]]) -> None:
    with request_context("rid-xyz"):
        bind(agent="product_rec", attempt=2)
        structlog.get_logger().info("agent.retry")
    assert captured[0]["agent"] == "product_rec"
    assert captured[0]["attempt"] == 2
    assert captured[0]["request_id"] == "rid-xyz"


@pytest.mark.anyio
async def test_context_propagates_into_gather(captured: list[dict[str, Any]]) -> None:
    """
    坑 3 的正面用例：gather 之前绑定的 request_id 会传播给两个子任务。
    这对应 supervisor.py 里两处 asyncio.gather。
    """
    log = structlog.get_logger()

    async def worker(name: str) -> None:
        await asyncio.sleep(0.01)
        log.info("agent.success", agent=name)

    with request_context("rid-gather"):
        await asyncio.gather(worker("user_profile"), worker("product_rec"))

    assert len(captured) == 2
    assert all(e["request_id"] == "rid-gather" for e in captured), (
        "子任务丢了 request_id —— 说明绑定发生在 gather 之后"
    )
    assert {e["agent"] for e in captured} == {"user_profile", "product_rec"}


@pytest.mark.anyio
async def test_child_bind_does_not_leak_to_sibling(captured: list[dict[str, Any]]) -> None:
    """
    坑 3 的反面用例：子任务里 bind 的字段【不该】泄漏给兄弟任务。

    这是 asyncio 任务复制上下文带来的正确语义 —— 我们希望
    agent= 这样的字段只标记它自己那次调用，而不是污染同请求的其他 Agent。
    """
    log = structlog.get_logger()

    async def worker(name: str) -> None:
        bind(agent=name)
        await asyncio.sleep(0.01)
        log.info("agent.success")

    with request_context("rid-iso"):
        await asyncio.gather(worker("user_profile"), worker("product_rec"))

    agents = sorted(e["agent"] for e in captured)
    assert agents == ["product_rec", "user_profile"], (
        f"两个子任务的 agent 字段串了: {agents}"
    )
    assert all(e["request_id"] == "rid-iso" for e in captured)


def test_scope_restores_on_exit(captured: list[dict[str, Any]]) -> None:
    """scope 是 bind 的作用域版：退出后绑定必须消失。"""
    log = structlog.get_logger()
    with request_context("rid-scope"):
        with scope(agent="marketing_copy"):
            log.info("inner")
        log.info("outer")  # 这行不该再有 agent

    assert captured[0]["agent"] == "marketing_copy"
    assert "agent" not in captured[1], (
        "scope 退出后 agent 仍然残留 —— 会污染调用方后续的日志"
    )
    assert captured[1]["request_id"] == "rid-scope", "scope 不该动请求级字段"


@pytest.mark.anyio
async def test_scope_does_not_leak_to_direct_caller(captured: list[dict[str, Any]]) -> None:
    """
    复现实测中踩到的泄漏：

    Phase 3 的 marketing_copy 是被【直接 await】的，和 supervisor 同一个 task。
    用裸 bind 的话，agent=marketing_copy 会挂到 supervisor.complete 上。
    scope 必须阻止这件事。
    """
    log = structlog.get_logger()

    async def callee() -> None:
        with scope(agent="marketing_copy"):
            await asyncio.sleep(0)
            log.info("agent.success")

    with request_context("rid-leak"):
        await callee()                     # 直接 await，不是 gather 子任务
        log.info("supervisor.complete")    # 调用方自己的日志

    assert captured[0]["agent"] == "marketing_copy"
    assert "agent" not in captured[1], (
        "被直接 await 的调用把 agent 字段泄漏给了调用方 —— 这正是实测踩到的 bug"
    )
    assert captured[1]["event"] == "supervisor.complete"
