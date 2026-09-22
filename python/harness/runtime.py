"""
Agent 运行时 —— 承载「这次调用还能不能打」这件事。

    为什么熔断器必须按【agent 名】索引，而不是挂在 agent 实例上
    ──────────────────────────────────────────────────────
    这个进程里【曾经】同时存在两套 agent 实例：
        orchestrator/graph.py          模块导入时构造 4 个
        orchestrator/supervisor.py     构造时又构造 4 个
    （现已统一走 harness/deps 的组合根。但"按名索引"这个设计保留 ——
     它让"实例有几个"不再是正确性的前提。）

    若熔断器挂在实例上，同一个逻辑 Agent 会有两份互不相干的健康状态：
    `/recommend` 打挂了 product_rec，`/recommend/graph` 完全不知情，
    继续把请求往一个已经确定要失败的下游上打 —— 熔断在最需要它的时候是错的。

    按名字索引到共享运行时上，实例有几个就不重要了。

调用顺序（这是本模块真正的工程内容）
──────────────────────────────────
    allow()  ->  熔断门，在【重试外层】
    wait_for ->  取消期限，也在【重试外层】
    retry    ->  单次调用内部的瞬时容错

    熔断放重试内层会：① 每个 attempt 都重新求值一次门禁，让重试风暴照样穿透；
    ② 统计的是"尝试失败数"而非"调用结果数"，把失败信号放大数倍。
    16 秒级的接口上，熔断的意义恰恰是【在花掉整个期限之前】短路掉注定失败的调用。
"""

from __future__ import annotations

from typing import Any

import structlog

from config import get_settings
from .breaker import CircuitBreaker

logger = structlog.get_logger()


class CircuitOpenError(Exception):
    """
    熔断器拒绝了这次调用。

    刻意做成独立类型（而不是 RuntimeError("circuit_open")）：
    调用方需要能区分"下游挂了"和"我们主动不打"，日志告警的分级也不同
    （前者要查下游，后者说明熔断在正常工作）。
    """

    def __init__(self, agent_name: str, error_rate: float = 0.0) -> None:
        self.agent_name = agent_name
        self.error_rate = error_rate
        super().__init__(f"circuit_open: {agent_name}")


class AgentRuntime:
    """进程级共享的 Agent 健康状态。"""

    def __init__(
        self,
        failure_threshold: int | None = None,
        window: int | None = None,
        reset_timeout_s: float | None = None,
    ) -> None:
        settings = get_settings()
        self.failure_threshold = (
            failure_threshold if failure_threshold is not None
            else settings.breaker_failure_threshold
        )
        self.window = window if window is not None else settings.breaker_window
        self.reset_timeout_s = (
            reset_timeout_s if reset_timeout_s is not None
            else settings.breaker_reset_timeout_s
        )
        self._breakers: dict[str, CircuitBreaker] = {}

    def breaker_for(self, agent_name: str) -> CircuitBreaker:
        """惰性创建，所以新增 Agent 不需要改这里。"""
        breaker = self._breakers.get(agent_name)
        if breaker is None:
            breaker = CircuitBreaker(
                name=agent_name,
                failure_threshold=self.failure_threshold,
                window=self.window,
                reset_timeout_s=self.reset_timeout_s,
            )
            self._breakers[agent_name] = breaker
        return breaker

    def allow(self, agent_name: str) -> bool:
        """
        熔断门。被拒绝时【不要】调用 record() ——
        若把"被熔断挡住"也记成失败，熔断器将永远无法闭合。
        """
        breaker = self.breaker_for(agent_name)
        allowed = breaker.allow()
        if not allowed:
            logger.warning(
                "agent.circuit_open",
                agent=agent_name,
                state=breaker.state,
                error_rate=round(breaker.error_rate, 3),
                failures_in_window=breaker.failures_in_window,
                opened_s_ago=(
                    round(breaker.opened_s_ago, 1)
                    if breaker.opened_s_ago is not None else None
                ),
            )
        return allowed

    def record(self, agent_name: str, ok: bool) -> None:
        """记录一次【调用】的最终结果（不是单次尝试的结果）。"""
        breaker = self.breaker_for(agent_name)
        was_closed = breaker.state == "closed"
        breaker.record(ok)
        if was_closed and breaker.state == "open":
            logger.error(
                "agent.circuit_tripped",
                agent=agent_name,
                failures_in_window=breaker.failures_in_window,
                window=self.window,
                cooldown_s=self.reset_timeout_s,
            )

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """给 /api/v1/metrics 用。"""
        return {name: b.snapshot() for name, b in self._breakers.items()}

    def reset(self, agent_name: str | None = None) -> None:
        """运维用：下游修好后手动复位。"""
        if agent_name is None:
            for b in self._breakers.values():
                b.reset()
        else:
            self.breaker_for(agent_name).reset()


_runtime: AgentRuntime | None = None


def get_runtime() -> AgentRuntime:
    """
    进程级单例。

    不用 @lru_cache 是因为测试需要能重置它 —— 熔断状态跨测试泄漏
    会让结果不可复现（本项目已经在 contextvars 上踩过一次这类问题）。
    """
    global _runtime
    if _runtime is None:
        _runtime = AgentRuntime()
    return _runtime


def reset_runtime() -> None:
    """丢弃单例。测试用。"""
    global _runtime
    _runtime = None
