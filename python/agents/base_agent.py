from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from typing import Any

import structlog
from tenacity import RetryCallState, retry, stop_after_attempt, wait_exponential

from harness import scope
from harness.runtime import CircuitOpenError, get_runtime
from models.schemas import AgentResult

logger = structlog.get_logger()


class BaseAgent(ABC):
    """All agents inherit from this base class with retry, timeout, and fallback."""

    def __init__(self, name: str, timeout: float = 10.0, max_attempts: int = 2):
        self.name = name
        self.timeout = timeout
        # 原名 max_retries，但 stop_after_attempt(N) 的含义是【总尝试次数】N，
        # 即重试 N-1 次。名字在骗人（docs/interview-guide.md 说"最多3次"，
        # 实际是 2 次）—— 改名不改行为。
        self.max_attempts = max_attempts
        self._call_count = 0
        self._error_count = 0

    @abstractmethod
    async def _execute(self, **kwargs: Any) -> AgentResult:
        """Core logic implemented by each concrete agent."""

    async def run(self, **kwargs: Any) -> AgentResult:
        """Public entry: wraps _execute with timing, retries, and fallback."""
        # 用 scope 而不是裸 bind —— 这一点是实测踩出来的：
        # 本 Agent 可能被【直接 await】（Phase 3 的 marketing_copy 就是），
        # 此时它和调用方共享同一个 task 上下文，裸 bind 会把
        # agent=marketing_copy 泄漏到 supervisor.complete 的日志上。
        # scope 退出时自动还原，绑定只覆盖本次调用。
        #
        # request_id 则不用管：它由编排器在 asyncio.gather 之前 bind，是请求级的，
        # 只要这里不覆盖它，本 Agent 内所有日志（包括一行都没改的那些）自动带上。
        with scope(agent=self.name):
            return await self._run_once(**kwargs)

    async def _run_once(self, **kwargs: Any) -> AgentResult:
        """
        一次完整调用：熔断门 -> 取消期限 -> 重试 -> 记账。

        顺序是刻意的，它就是这个 harness 层的核心工程内容：
            熔断在最外层（按【调用】计数，且能在花掉期限之前短路）
            期限包住重试（self.timeout 是总预算，不是单次尝试预算）
            重试在最内层（只针对瞬时故障）
        """
        start = time.perf_counter()
        runtime = get_runtime()
        self._call_count += 1

        # 熔断门。被拒绝时【不】调用 runtime.record —— 把"被挡住"记成失败
        # 会让熔断器永远无法闭合。
        if not runtime.allow(self.name):
            breaker = runtime.breaker_for(self.name)
            latency_ms = (time.perf_counter() - start) * 1000
            return self._fallback(
                latency_ms, CircuitOpenError(self.name, breaker.error_rate)
            )

        try:
            result = await asyncio.wait_for(
                self._retry_execute(**kwargs), timeout=self.timeout
            )
            result.latency_ms = (time.perf_counter() - start) * 1000
            runtime.record(self.name, True)
            logger.info(
                "agent.success",
                agent=self.name,
                latency_ms=round(result.latency_ms, 1),
            )
            return result
        except TimeoutError:
            # 3.11+ 下 asyncio.wait_for 抛的就是内置 TimeoutError（属于 Exception），
            # 所以原有的 except Exception 本来就能接住 —— 单独一支只是为了
            # 让超时能和"LLM 返回 500"在日志里区分开。
            self._error_count += 1
            latency_ms = (time.perf_counter() - start) * 1000
            logger.error(
                "agent.timeout",
                agent=self.name,
                timeout_s=self.timeout,
                latency_ms=round(latency_ms, 1),
            )
            # 先记原因再记结果：runtime.record 可能触发 agent.circuit_tripped，
            # 若反过来，日志里"跳闸"会排在"超时"前面，看起来像是无缘无故跳的。
            runtime.record(self.name, False)
            return self._fallback(
                latency_ms,
                TimeoutError(f"{self.name} exceeded {self.timeout}s budget"),
            )
        except Exception as exc:
            self._error_count += 1
            latency_ms = (time.perf_counter() - start) * 1000
            logger.error("agent.failed", agent=self.name, error=str(exc))
            runtime.record(self.name, False)
            return self._fallback(latency_ms, exc)

    async def _retry_execute(self, **kwargs: Any) -> AgentResult:
        @retry(
            stop=stop_after_attempt(self.max_attempts),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
            reraise=True,
            before_sleep=self._on_retry,
        )
        async def _inner():
            return await self._execute(**kwargs)

        return await _inner()

    def _on_retry(self, retry_state: RetryCallState) -> None:
        """
        在一次尝试失败、即将退避重试时记一笔。

        之前这里什么都没有 —— 一次重试是【完全不可见】的：
        agent.success 和 agent.failed 之间没有任何日志，而成功重试后
        run() 记的又是两次尝试的【总】耗时。结果是"这个请求为什么花了
        63 秒"根本无法回答，也无法区分"模型慢"和"在重试"。
        """
        outcome = retry_state.outcome
        exc = outcome.exception() if outcome is not None else None
        next_action = retry_state.next_action
        logger.warning(
            "agent.retry",
            attempt=retry_state.attempt_number,
            max_attempts=self.max_attempts,
            next_wait_s=round(getattr(next_action, "sleep", 0.0), 2),
            error=str(exc) if exc is not None else None,
            error_type=type(exc).__name__ if exc is not None else None,
        )

    def _fallback(self, latency_ms: float, exc: Exception) -> AgentResult:
        """Return a degraded but valid result when the agent fails."""
        return AgentResult(
            agent_name=self.name,
            success=False,
            latency_ms=latency_ms,
            error=str(exc),
            confidence=0.0,
        )

    @property
    def error_rate(self) -> float:
        """
        委托给共享熔断器的【滑动窗口】错误率。

        原实现是累计比率（_error_count / _call_count），单调不降：
        长跑进程里一旦出错就永远回不到低位，拿去驱动熔断会导致
        熔断打开后无法闭合。而且它此前从没被任何地方调用过 —— 是个死属性。

        保留 _call_count / _error_count 只是为了兼容既有读取方，
        它们不再是健康状态的来源。
        """
        return get_runtime().breaker_for(self.name).error_rate
