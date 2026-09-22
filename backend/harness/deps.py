"""
组合根 —— 全进程共享单例。

    为什么需要这个文件
    ──────────────────
    接 harness 之前，这个进程里同时存在【互相不知情】的两套 Agent：
        orchestrator/graph.py         模块导入时构造 4 个 Agent
        orchestrator/supervisor.py    __init__ 里再构造 4 个 Agent

    后果是真实存在的 bug，不只是代码不好看：在一个端点上打挂的 Agent，
    另一个端点完全不知情 —— 熔断状态各算各的。

    （当时还有第三个分裂源：A/B 引擎。它已随 A/B 引擎整体移除，
    相关的实例分裂问题也就不存在了。）

    合并成一份之后，连带修好两件事：
        1. 熔断状态共享（一个端点打挂的 Agent，另一个端点也知道）
        2. 指标统计共享

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
    from services.feature_store import FeatureStore
    from services.metrics import MetricsCollector

    from .pricing import PricingTable


@lru_cache(maxsize=1)
def get_feature_store() -> "FeatureStore | None":
    """
    实时特征存储单例。**开关关闭时返回 None。**

    为什么返回 None 而不是"永远返回一个 store、由 store 自己看开关"：
    关掉时连 redis 客户端都不构造 —— 和 InventoryAgent 的 MCP 客户端
    同一个口径（关掉时 `self.db = None`，连 `import mcp` 的开销都省掉），
    也就是"关闭时零新增失败面"。

    注意它和 `get_agents()` 一样是【构造时】决定开关的：改了
    ECOM_FEATURE_STORE_ENABLED 必须重启进程。外部依赖不该在请求中途被换掉。
    """
    from config import get_settings

    settings = get_settings()
    if not settings.feature_store_enabled:
        return None

    from redis.asyncio import Redis

    from services.feature_store import FeatureStore

    client = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        # ⚠️ protocol=2 是【必须】的，不是可选优化：
        # 本机 6379 上是原生 Redis 5.0，它不支持 HELLO 命令，而
        # redis-py 5+ 默认走 RESP3、建连时先发 HELLO —— 对 5.0 直接
        # 抛 `ResponseError: unknown command 'HELLO'`。
        # RESP2 在 Redis 7.x 上同样合法，所以这不是"只在本机能跑的 hack"。
        protocol=2,
    )
    return FeatureStore(
        client,
        ttl=settings.feature_ttl_seconds,
        window_days=settings.feature_window_days,
        timeout_s=settings.feature_store_timeout_s,
        tz_offset_hours=settings.feature_tz_offset_hours,
    )


@lru_cache(maxsize=1)
def get_metrics_collector() -> "MetricsCollector":
    from services.metrics import MetricsCollector

    return MetricsCollector()


@lru_cache(maxsize=1)
def get_pricing() -> "PricingTable":
    """
    价格表单例。

    只构造一次的理由：价格表支持从环境变量 / JSON 文件加载，
    每次都读盘没必要；而且评测报告里要记它的【指纹】——
    如果每次构造出来的指纹都可能不同，"优化前 vs 优化后"的成本对比就是假的。
    """
    from .pricing import PricingTable

    return PricingTable.from_env()


@lru_cache(maxsize=1)
def get_supervisor() -> "SupervisorOrchestrator":
    from orchestrator.supervisor import SupervisorOrchestrator

    return SupervisorOrchestrator()


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


_tool_registry: Any = None


async def get_tool_registry():
    """
    工具注册表单例（惰性、异步）。

    为什么不能用 @lru_cache：装配它需要【异步】去问 MCP Server 有哪些工具
    （list_tools 是一次真实的 stdio 往返）。lru_cache 不支持 async。

    未装配 MCP 时只返回内置工具 —— Copilot 照样能回答"系统现在健康吗"，
    只是不能查库存。**少几个工具，而不是整个功能不可用。**
    """
    global _tool_registry
    if _tool_registry is None:
        from .tools import build_registry

        _tool_registry = await build_registry()
    return _tool_registry


def reset_deps() -> None:
    """丢弃全部单例。测试用。"""
    global _tool_registry
    _tool_registry = None
    get_agents.cache_clear()
    get_supervisor.cache_clear()
    get_feature_store.cache_clear()
    get_metrics_collector.cache_clear()
    get_pricing.cache_clear()
