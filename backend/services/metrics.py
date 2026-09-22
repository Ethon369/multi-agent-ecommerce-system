"""
监控指标收集
- Agent调用成功率 / 延迟
- 推荐CTR / CVR / GMV
- A/B测试实验指标
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentMetric:
    call_count: int = 0
    success_count: int = 0
    total_latency_ms: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        return self.success_count / self.call_count if self.call_count else 0.0

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / self.call_count if self.call_count else 0.0


class MetricsCollector:
    """In-memory metrics collector; swap to Prometheus in production."""

    def __init__(self):
        self._agent_metrics: dict[str, AgentMetric] = defaultdict(AgentMetric)
        self._business_events: list[dict[str, Any]] = []

        # LLM 用量累计（进程启动以来）
        self._llm_calls = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._reasoning_tokens = 0
        self._cached_tokens = 0
        self._cost_usd = 0.0
        self._cost_unknown = False
        self._llm_by_agent: dict[str, dict[str, int]] = defaultdict(dict)

    def record_agent_call(self, agent_name: str, success: bool, latency_ms: float, error: str = ""):
        m = self._agent_metrics[agent_name]
        m.call_count += 1
        if success:
            m.success_count += 1
        m.total_latency_ms += latency_ms
        if error:
            m.errors.append(error)

    def record_business_event(self, event_type: str, **kwargs: Any):
        """Record CTR/CVR/GMV events for analytics."""
        self._business_events.append({
            "type": event_type,
            "timestamp": time.time(),
            **kwargs,
        })

    def get_agent_stats(self) -> dict[str, dict[str, Any]]:
        result = {}
        for name, m in self._agent_metrics.items():
            result[name] = {
                "call_count": m.call_count,
                "success_rate": round(m.success_rate, 4),
                "avg_latency_ms": round(m.avg_latency_ms, 1),
                "recent_errors": m.errors[-5:],
            }
        return result

    def get_business_stats(self) -> dict[str, Any]:
        if not self._business_events:
            return {}
        by_type: dict[str, list[dict]] = defaultdict(list)
        for e in self._business_events:
            by_type[e["type"]].append(e)
        stats = {}
        for t, events in by_type.items():
            stats[t] = {"count": len(events)}
        return stats

    # ── LLM 用量累计 ────────────────────────────────────────────
    #
    # 与 AgentMetric 的区别：那个统计"延迟与成功率"（每次请求都有），
    # 这个统计"花了多少 token 和钱"（进程启动以来的累计值）。
    #
    # 注意 cost_usd 用 None 表示"有模型查不到单价"，不是 0。
    # 累计值里只要有【任何一次】算不出来，整体就是 None ——
    # 报一个"部分和"会让人以为成本比实际低。

    def record_llm_usage(self, usage_report: dict[str, Any]) -> None:
        self._llm_calls += int(usage_report.get("llm_calls") or 0)
        self._input_tokens += int(usage_report.get("input_tokens") or 0)
        self._output_tokens += int(usage_report.get("output_tokens") or 0)
        self._reasoning_tokens += int(usage_report.get("reasoning_tokens") or 0)
        self._cached_tokens += int(usage_report.get("cached_tokens") or 0)

        cost = usage_report.get("cost_usd")
        if cost is None:
            # 有一次算不出来，累计值就不再可信
            self._cost_unknown = True
        else:
            self._cost_usd += float(cost)

        for agent, v in (usage_report.get("by_agent") or {}).items():
            bucket = self._llm_by_agent[agent]
            for k in ("calls", "input_tokens", "output_tokens", "reasoning_tokens"):
                bucket[k] = bucket.get(k, 0) + int(v.get(k) or 0)

    def get_llm_stats(self) -> dict[str, Any]:
        return {
            "llm_calls": self._llm_calls,
            "input_tokens": self._input_tokens,
            "output_tokens": self._output_tokens,
            "reasoning_tokens": self._reasoning_tokens,
            "cached_tokens": self._cached_tokens,
            "cost_usd": None if self._cost_unknown else round(self._cost_usd, 6),
            "cost_known": not self._cost_unknown,
            "by_agent": dict(self._llm_by_agent),
        }
