"""
token 与成本账本。

    为什么需要一个「账本」而不是随手打印
    ──────────────────────────────────
    LLM 应用的成本是隐性的：你不像调用数据库那样能感觉到"这次查询花了多少"。
    一次推荐调 3~5 次 LLM，每次都吞几千个 token，只有把账记下来才知道
    「一次推荐到底花多少钱」。

    记下来之后能回答三个问题：
      1. 哪个 Agent 最贵？      -> 优化有方向
      2. 缓存命中了吗？          -> 命中价是未命中的 1/50（实测差价）
      3. 这次改动让成本涨了没？  -> 需要前后对比

    并发正确性（这是本模块最容易写错的地方）
    ──────────────────────────────────────
    编排器用 asyncio.gather 让两个 Agent 同时跑，它们会同时往账本里记数。
    正确性依赖两个事实：

      ① asyncio 任务在【创建时复制上下文】。
         所以账本必须在 gather 【之前】放进 contextvar，
         子任务才能拿到【同一个对象引用】。

      ② 放进去的必须是【可变对象】，而不是一个值。
         子任务通过 ledger.add(...) 修改这个对象的属性。
         由于事件循环是单线程、且读写之间没有 await，+= 是安全的。

    ⚠️ 由此得出一条必须遵守的规则：
       读写账本之间【永远不许有 await】。
       这里的失败模式是"跨 await 的交错"，不是真并行 ——
       所以加 asyncio.Lock 是没用的（也拦不住），
       真正要做的是不在读写之间让出控制权。

    ⚠️ 另一条：账本绝不能做成模块级全局变量。
       两个并发请求会把 A 的 token 静默算进 B 的响应里，只在有负载时才现形。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator

import structlog

from .pricing import PricingTable

logger = structlog.get_logger()


@dataclass
class AgentUsage:
    """单个 Agent 的累计用量。"""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    # reasoning token 是【包含在】output_tokens 里的（实测 output_details.reasoning
    # 是 output_tokens 的子集），所以它只用于观测，不重复计入成本。
    reasoning_tokens: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }


@dataclass
class UsageAccumulator:
    """一次请求的账本。由编排器创建，贯穿该请求的全部 Agent 调用。"""

    by_agent: dict[str, AgentUsage] = field(default_factory=dict)
    by_model: dict[str, AgentUsage] = field(default_factory=dict)
    llm_calls: int = 0
    estimated: bool = False
    """True 表示至少有一次用量是【估算】的（API 没返回 usage），不是实测。"""

    def _bucket(self, table: dict[str, AgentUsage], key: str) -> AgentUsage:
        bucket = table.get(key)
        if bucket is None:
            bucket = AgentUsage()
            table[key] = bucket
        return bucket

    def record(
        self,
        agent_name: str,
        model: str,
        usage_metadata: dict[str, Any] | None,
        *,
        estimated: bool = False,
    ) -> None:
        """
        记一次 LLM 调用。

        ⚠️ 调用方必须保证：从进入本方法到离开，中间【没有 await】。
        见模块 docstring。
        """
        self.llm_calls += 1
        if estimated:
            self.estimated = True

        u = usage_metadata or {}
        input_tokens = int(u.get("input_tokens") or 0)
        output_tokens = int(u.get("output_tokens") or 0)
        cached = int((u.get("input_token_details") or {}).get("cache_read") or 0)
        reasoning = int((u.get("output_token_details") or {}).get("reasoning") or 0)

        for table, key in ((self.by_agent, agent_name), (self.by_model, model or "unknown")):
            b = self._bucket(table, key)
            b.calls += 1
            b.input_tokens += input_tokens
            b.output_tokens += output_tokens
            b.cached_tokens += cached
            b.reasoning_tokens += reasoning

    @property
    def input_tokens(self) -> int:
        return sum(b.input_tokens for b in self.by_agent.values())

    @property
    def output_tokens(self) -> int:
        return sum(b.output_tokens for b in self.by_agent.values())

    @property
    def cached_tokens(self) -> int:
        return sum(b.cached_tokens for b in self.by_agent.values())

    @property
    def reasoning_tokens(self) -> int:
        return sum(b.reasoning_tokens for b in self.by_agent.values())

    def cost_usd(self, pricing: PricingTable) -> float | None:
        """
        总成本（美元）。**任一模型查不到价格就返回 None，不猜。**

        宁可整体显示 null，也不要"部分能算就报个部分和" ——
        那样会得到一个看起来精确、实际漏算的数字。
        """
        if not self.by_model:
            return 0.0

        total = 0.0
        for model, b in self.by_model.items():
            c = pricing.cost(model, b.input_tokens, b.output_tokens, b.cached_tokens)
            if c is None:
                return None
            total += c
        return total

    def as_report(self, pricing: PricingTable) -> dict[str, Any]:
        """给 HTTP 响应用的可序列化结构。"""
        cost = self.cost_usd(pricing)
        return {
            "llm_calls": self.llm_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cost_usd": None if cost is None else round(cost, 6),
            "cost_known": cost is not None,
            "estimated": self.estimated,
            "by_agent": {k: v.as_dict() for k, v in self.by_agent.items()},
            "by_model": {k: v.as_dict() for k, v in self.by_model.items()},
        }


# 当前请求的账本。
#
# 注意这里存的是【对象的引用】，不是值本身 ——
# asyncio 子任务复制上下文时复制的是这个引用，所以父子任务操作的是同一个对象。
# 也正因如此，子任务里绝不能写 _current.set(新账本)：那只会影响它自己。
_current: ContextVar[UsageAccumulator | None] = ContextVar("harness_usage", default=None)


def current_usage() -> UsageAccumulator | None:
    """取当前请求的账本；不在请求上下文里时返回 None（不报错）。"""
    return _current.get()


def record_usage(
    agent_name: str,
    model: str,
    usage_metadata: dict[str, Any] | None,
    *,
    estimated: bool = False,
) -> None:
    """
    记一次调用。没有活跃账本时静默忽略 ——
    这样单独跑一个 Agent（比如写单测）不需要先建账本。
    """
    acc = _current.get()
    if acc is None:
        return
    acc.record(agent_name, model, usage_metadata, estimated=estimated)


@contextmanager
def usage_scope() -> Iterator[UsageAccumulator]:
    """
    开一个账本作用域。

    ⚠️ 必须在 asyncio.gather 【之前】进入 —— 否则子任务拿不到同一个账本。
    """
    acc = UsageAccumulator()
    token = _current.set(acc)
    try:
        yield acc
    finally:
        _current.reset(token)
