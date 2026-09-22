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

import asyncio
import time
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


# ── M2：真超时 ──────────────────────────────────────────────

class SlowAgent(BaseAgent):
    """永远睡下去，用来触发超时。"""

    def __init__(self, sleep_s: float = 5.0, **kw: Any):
        kw.setdefault("timeout", 0.1)
        super().__init__(name="slow", **kw)
        self.sleep_s = sleep_s

    async def _execute(self, **kwargs: Any) -> AgentResult:
        await asyncio.sleep(self.sleep_s)
        return AgentResult(agent_name=self.name)


@pytest.mark.anyio
async def test_timeout_is_actually_enforced(captured: list[dict[str, Any]]) -> None:
    """
    回归守卫：self.timeout 曾经是【死字段】—— 赋值了但从没被读过，
    settings 里四个 agent_timeout_* 因此完全无效。
    """
    agent = SlowAgent(sleep_s=5.0, timeout=0.1, max_attempts=1)

    t0 = time.perf_counter()
    result = await agent.run()
    elapsed = time.perf_counter() - t0

    assert elapsed < 2.0, f"超时没生效，实际等了 {elapsed:.2f}s"
    assert result.success is False
    assert "budget" in (result.error or "")

    events = [e["event"] for e in captured]
    assert "agent.timeout" in events, (
        "超时必须单独记 agent.timeout，否则和'LLM 返回 500'分不开"
    )
    timeout_ev = next(e for e in captured if e["event"] == "agent.timeout")
    assert timeout_ev["timeout_s"] == 0.1
    assert timeout_ev["agent"] == "slow"


@pytest.mark.anyio
async def test_timeout_budget_covers_all_retries() -> None:
    """
    self.timeout 是【整个 run() 的总预算】，不是单次尝试预算。
    否则最坏情况 2*timeout + 退避 会超过运维写在配置里的数字。
    """
    agent = SlowAgent(sleep_s=5.0, timeout=0.3, max_attempts=3)

    t0 = time.perf_counter()
    result = await agent.run()
    elapsed = time.perf_counter() - t0

    assert elapsed < 1.5, (
        f"总预算没兜住重试：配置 0.3s，实际耗时 {elapsed:.2f}s"
    )
    assert result.success is False


# ── M2：熔断 ────────────────────────────────────────────────

@pytest.fixture
def tight_breaker():
    """把熔断阈值调小，便于在小样本里触发。必须在首次 breaker_for 之前设置。"""
    from harness import get_runtime

    rt = get_runtime()
    rt.failure_threshold = 2
    rt.window = 10
    rt.reset_timeout_s = 0.05
    return rt


@pytest.mark.anyio
async def test_breaker_opens_and_short_circuits(captured: list[dict[str, Any]], tight_breaker) -> None:
    agent = AlwaysFailsAgent(timeout=5.0, max_attempts=1)

    await agent.run()   # 第 1 次失败
    await agent.run()   # 第 2 次失败 -> 达到阈值，熔断打开
    assert tight_breaker.breaker_for("always_fails").state == "open"

    attempts_before = agent.attempts
    captured.clear()

    t0 = time.perf_counter()
    result = await agent.run()   # 第 3 次：应被熔断挡住
    elapsed = time.perf_counter() - t0

    assert elapsed < 0.05, f"熔断没有短路，白等了 {elapsed:.3f}s"
    assert agent.attempts == attempts_before, "被熔断挡住却仍然打了下游"
    assert result.success is False
    assert "circuit_open" in (result.error or "")
    assert "agent.circuit_open" in [e["event"] for e in captured]


@pytest.mark.anyio
async def test_circuit_open_does_not_count_as_failure(tight_breaker) -> None:
    """
    被熔断挡住的调用【不能】记成失败 —— 否则熔断器永远无法闭合，
    因为每一次拒绝都会再喂给它一个失败。
    """
    agent = AlwaysFailsAgent(timeout=5.0, max_attempts=1)
    await agent.run()
    await agent.run()

    breaker = tight_breaker.breaker_for("always_fails")
    assert breaker.state == "open"

    for _ in range(20):
        await agent.run()

    assert breaker.state == "open", "状态不该被拒绝调用改动"
    assert agent.attempts == 2, f"被挡住时不该真的调用下游，实际调了 {agent.attempts} 次"


@pytest.mark.anyio
async def test_breaker_recovers_after_cooldown(captured: list[dict[str, Any]], tight_breaker) -> None:
    """冷却期后放行一次探测；探测成功即闭合。"""
    agent = AlwaysFailsAgent(timeout=5.0, max_attempts=1)
    await agent.run()
    await agent.run()
    assert tight_breaker.breaker_for("always_fails").state == "open"

    await asyncio.sleep(0.06)

    class NowFineAgent(AlwaysFailsAgent):
        async def _execute(self, **kwargs: Any) -> AgentResult:
            self.attempts += 1
            return AgentResult(agent_name=self.name, success=True)

    good = NowFineAgent(timeout=5.0, max_attempts=1)
    result = await good.run()

    assert result.success is True
    assert tight_breaker.breaker_for("always_fails").state == "closed", (
        "探测成功后熔断器必须闭合"
    )


@pytest.mark.anyio
async def test_error_rate_delegates_to_breaker(tight_breaker) -> None:
    """
    error_rate 现在是【滑动窗口】比率，而不是原来那个累计且永不回落的版本。
    """
    agent = AlwaysFailsAgent(timeout=5.0, max_attempts=1)

    assert agent.error_rate == 0.0

    class HalfFailsAgent(AlwaysFailsAgent):
        def __init__(self, **kw: Any):
            super().__init__(**kw)
            self.n = 0

        async def _execute(self, **kwargs: Any) -> AgentResult:
            self.n += 1
            if self.n % 2:
                raise RuntimeError("odd failure")
            return AgentResult(agent_name=self.name, success=True)

    half = HalfFailsAgent(timeout=5.0, max_attempts=1)
    await half.run()   # 失败
    await half.run()   # 成功
    assert half.error_rate == 0.5, "2 次调用 1 次失败 = 0.5"
