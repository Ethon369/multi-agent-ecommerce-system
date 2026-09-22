"""
两个编排器共用的响应组装。

    为什么单独一个文件
    ────────────────
    `supervisor.py` 与 `graph.py` 是同一件事的两套实现（一个 asyncio.gather、
    一个 LangGraph），它们必须产出**同一个响应契约** —— 前端只认一套结构，
    评测集也只写一套断言。HarnessReport 的组装逻辑原本只写在
    `SupervisorOrchestrator._harness_report()` 里，graph 那条路径复用它
    只能靠 import 一个编排器的私有静态方法，那是错的方向依赖
    （编排器 A 的实现细节被编排器 B 依赖）。

    挪到这里之后："怎么把一次请求的运行时状态汇总成报告"只在一个地方定义，
    两条路径的差异就只剩编排方式本身 —— 这正是可以把它们拿来对比的前提。
"""

from __future__ import annotations

from typing import Any

from models.schemas import HarnessReport, HarnessUsageReport

from harness.runtime import get_runtime


def build_harness_report(
    results: dict[str, Any],
    usage_report: dict[str, Any],
) -> HarnessReport:
    """
    把运行时保障层的状态汇总成一份自述报告。

    results 的 key 是 agent 名、value 是 AgentResult（或其子类）；
    两条编排路径都要按同样的键名传进来，否则前端看到的话术会两边不一致
    （见 graph.py 里 build_response 的键名归一化）。
    """
    runtime = get_runtime()
    return HarnessReport(
        usage=HarnessUsageReport(**usage_report),
        agents={
            name: {
                "success": r.success,
                "latency_ms": round(r.latency_ms, 1),
                "confidence": r.confidence,
                "error": r.error,
                "breaker_state": runtime.breaker_for(name).state,
            }
            for name, r in results.items()
        },
        breakers=runtime.snapshot(),
    )


#: 响应里 agent_results 使用的规范键名。
#:
#: 它是"契约的一部分"，所以写在这里而不是散在两处：
#: supervisor 直接用它组装，graph 用它做归一化。
#: 注意 supervisor 里 product_rec 是【重排后】的结果（Phase 2），
#: 不是 Phase 1 的首次召回 —— 首次召回是中间产物，不进响应。
CANONICAL_AGENT_KEYS = ("user_profile", "product_rec", "marketing_copy", "inventory")
