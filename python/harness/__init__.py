"""
harness —— Agent 运行时骨架。

这一层负责「承载 LLM 的那部分工程」，而不是「让 LLM 干什么」：
请求级追踪、超时、熔断、重试、降级、token 记账、工具分发。

设计边界（写给未来的自己和面试官）：
    harness 是可被【确定性主链路】import 的运行时。
    它不包含任何「由模型自主决定做什么」的逻辑 —— 那种东西在 copilot/ 里。
    这条边界是刻意的：主推荐链路必须保持确定性，不该引入 ReAct 的不确定性。
"""

from __future__ import annotations

from .breaker import CircuitBreaker
from .llm import build_chat_model
from .runtime import AgentRuntime, CircuitOpenError, get_runtime, reset_runtime
from .trace import (
    bind,
    current_request_id,
    new_request_id,
    request_context,
    scope,
)

__all__ = [
    "AgentRuntime",
    "CircuitBreaker",
    "CircuitOpenError",
    "bind",
    "build_chat_model",
    "current_request_id",
    "get_runtime",
    "new_request_id",
    "request_context",
    "reset_runtime",
    "scope",
]
