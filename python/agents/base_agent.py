from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any

import structlog
from tenacity import RetryCallState, retry, stop_after_attempt, wait_exponential

from harness import scope
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
        start = time.perf_counter()
        self._call_count += 1

        try:
            result = await self._retry_execute(**kwargs)
            result.latency_ms = (time.perf_counter() - start) * 1000
            logger.info(
                "agent.success",
                agent=self.name,
                latency_ms=round(result.latency_ms, 1),
            )
            return result
        except Exception as exc:
            self._error_count += 1
            latency_ms = (time.perf_counter() - start) * 1000
            logger.error("agent.failed", agent=self.name, error=str(exc))
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
        if self._call_count == 0:
            return 0.0
        return self._error_count / self._call_count
