"""
BaseAgent 的 harness 行为测试 —— 全部离线，不碰网络。

这里测的是 harness 层的【承诺】：
  - 一次重试必须留下 agent.retry 记录（此前完全不可见）
  - 失败必须降级成 AgentResult 而不是抛异常
  - 被直接 await 时不能把 agent 字段泄漏给调用方

为什么重试可见性值得单独一个测试：
    实测中 product_rec 有一次 run() 花了 63 秒。没有 agent.retry 事件时，
    无法区分"模型慢"和"在重试"，只能靠猜 —— 而且猜错了（实际没有重试）。
    这个测试保证以后不会再靠猜。
"""

from __future__ import annotations

from typing import Any

import pytest

from agents.base_agent import BaseAgent
from harness import request_context
from models.schemas import AgentResult


class FlakyAgent(BaseAgent):
    """按脚本失败若干次再成功的假 Agent。"""

    def __init__(self, fail_times: int = 0, **kw: Any):
        super().__init__(name="flaky", **kw)
        self.fail_times = fail_times
        self.attempts = 0

    async def _execute(self, **kwargs: Any) -> AgentResult:
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise RuntimeError(f"boom-{self.attempts}")
        return AgentResult(agent_name=self.name, success=True)


class AlwaysFailsAgent(BaseAgent):
    def __init__(self, **kw: Any):
        super().__init__(name="always_fails", **kw)
        self.attempts = 0

    async def _execute(self, **kwargs: Any) -> AgentResult:
        self.attempts += 1
        raise RuntimeError("permanent failure")


# 退避最小 0.5s，测试里把等待压到最短
FAST = {"timeout": 5.0, "max_attempts": 2}


@pytest.mark.anyio
async def test_no_retry_when_first_attempt_succeeds(captured: list[dict[str, Any]]) -> None:
    agent = FlakyAgent(fail_times=0, **FAST)
    result = await agent.run()

    assert result.success is True
    assert agent.attempts == 1
    retries = [e for e in captured if e["event"] == "agent.retry"]
    assert retries == [], "成功的首次尝试不该产生 agent.retry"


@pytest.mark.anyio
async def test_retry_event_is_emitted(captured: list[dict[str, Any]]) -> None:
    """核心：一次重试必须留下痕迹。"""
    agent = FlakyAgent(fail_times=1, **FAST)
    result = await agent.run()

    assert result.success is True, "第二次尝试应成功"
    assert agent.attempts == 2

    retries = [e for e in captured if e["event"] == "agent.retry"]
    assert len(retries) == 1, f"期望 1 次重试记录，实际 {len(retries)}"

    ev = retries[0]
    assert ev["agent"] == "flaky"
    assert ev["attempt"] == 1, "记录的是【刚失败的那次】尝试序号"
    assert ev["max_attempts"] == 2
    assert ev["error"] == "boom-1"
    assert ev["error_type"] == "RuntimeError"
    assert "next_wait_s" in ev, "必须记下即将等待多久，否则无法解释总耗时"
    assert ev["next_wait_s"] >= 0


@pytest.mark.anyio
async def test_exhausted_retries_fall_back(captured: list[dict[str, Any]]) -> None:
    """重试耗尽后必须降级成 AgentResult，而不是把异常抛给编排器。"""
    agent = AlwaysFailsAgent(**FAST)
    result = await agent.run()

    assert result.success is False
    assert result.confidence == 0.0
    assert result.agent_name == "always_fails"
    assert "permanent failure" in (result.error or "")
    assert result.latency_ms > 0

    assert [e["event"] for e in captured if e["event"].startswith("agent.")] == [
        "agent.retry",
        "agent.failed",
    ]


@pytest.mark.anyio
async def test_max_attempts_semantics(captured: list[dict[str, Any]]) -> None:
    """max_attempts 是【总尝试次数】，不是"重试次数"。名字必须诚实。"""
    agent = AlwaysFailsAgent(timeout=5.0, max_attempts=3)
    await agent.run()
    assert agent.attempts == 3, f"max_attempts=3 应该是总共 3 次尝试，实际 {agent.attempts}"
    assert len([e for e in captured if e["event"] == "agent.retry"]) == 2, "重试 2 次"


@pytest.mark.anyio
async def test_agent_does_not_leak_agent_field_to_caller(
    captured: list[dict[str, Any]]
) -> None:
    """
    计划里没预料到、实测踩到的坑：
    被【直接 await】的 Agent 和调用方共享 task 上下文，
    用裸 bind 的话 agent= 会泄漏到调用方后续日志上。
    """
    import structlog

    log = structlog.get_logger()
    agent = FlakyAgent(fail_times=0, **FAST)

    with request_context("rid-agent-leak"):
        await agent.run()
        log.info("supervisor.complete")

    assert captured[0]["event"] == "agent.success"
    assert captured[0]["agent"] == "flaky"
    assert "agent" not in captured[1], "Agent 把 agent 字段泄漏给了调用方"
    assert captured[1]["request_id"] == "rid-agent-leak"


@pytest.mark.anyio
async def test_agent_logs_carry_request_id(captured: list[dict[str, Any]]) -> None:
    """既有日志语句一行没改，也要自动带上 request_id。"""
    agent = FlakyAgent(fail_times=0, **FAST)

    with request_context("rid-carry"):
        await agent.run()

    assert captured[0]["request_id"] == "rid-carry"
    assert captured[0]["agent"] == "flaky"
    assert "latency_ms" in captured[0]
