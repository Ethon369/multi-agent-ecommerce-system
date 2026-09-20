"""
组合根 —— 全进程共享单例。

    为什么需要这个文件
    ──────────────────
    接 harness 之前，这个进程里同时存在【互相不知情】的多套实例：
        orchestrator/graph.py:51-55   模块导入时构造 4 个 Agent + 1 个 ABTestEngine
        orchestrator/supervisor.py    __init__ 里再构造 4 个 Agent
        main.py                      又建了 1 个 ABTestEngine

    后果是真实存在的 bug，不只是代码不好看：
        POST /api/v1/experiments/{id}/outcome 把结果记进 main.py 的引擎，
        而 /api/v1/recommend/graph 的 Thompson 采样读的是 graph.py 自己的引擎
        —— 实验结论永远传不到那条路径上去。

    合并成一份之后，连带修好三件事：
        1. 熔断状态共享（一个端点打挂的 Agent，另一个端点也知道）
        2. A/B 实验结果共享
        3. 指标统计共享

    为什么用 @lru_cache 而不是模块级全局变量
    ──────────────────────────────────────
    模块级全局在 import 时就会构造全部 Agent（包括读 .env、建 LLM 客户端）。
    而 MCP Server 进程、测试进程未必需要全部依赖。
    @lru_cache 是惰性的：用到哪个建哪个。
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agents import InventoryAgent, MarketingCopyAgent, ProductRecAgent, UserProfileAgent
    from orchestrator.supervisor import SupervisorOrchestrator
    from services.ab_test import ABTestEngine
    from services.metrics import MetricsCollector


@lru_cache(maxsize=1)
def get_ab_engine() -> "ABTestEngine":
    from services.ab_test import ABTestEngine

    return ABTestEngine()


@lru_cache(maxsize=1)
def get_metrics_collector() -> "MetricsCollector":
    from services.metrics import MetricsCollector

    return MetricsCollector()


@lru_cache(maxsize=1)
def get_supervisor() -> "SupervisorOrchestrator":
    from orchestrator.supervisor import SupervisorOrchestrator

    return SupervisorOrchestrator(ab_engine=get_ab_engine())


@lru_cache(maxsize=1)
def get_agents() -> dict[str, Any]:
    """
    四个 Agent 的单例集合，按名字索引。

    注意 InventoryAgent 的 MCP 客户端是在构造时按开关决定的 ——
    所以改了 ECOM_MCP_WMS_ENABLED 必须重启进程，改不了运行时。
    这是有意的：MCP 客户端是外部依赖，不该在请求中途被换掉。
    """
    from agents import (
        InventoryAgent,
        MarketingCopyAgent,
        ProductRecAgent,
        UserProfileAgent,
    )

    return {
        "user_profile": UserProfileAgent(),
        "product_rec": ProductRecAgent(),
        "marketing_copy": MarketingCopyAgent(),
        "inventory": InventoryAgent(),
    }


def reset_deps() -> None:
    """丢弃全部单例。测试用。"""
    get_agents.cache_clear()
    get_supervisor.cache_clear()
    get_ab_engine.cache_clear()
    get_metrics_collector.cache_clear()
