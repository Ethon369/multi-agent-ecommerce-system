"""
组合根测试 —— 锁住"全进程一份实例"这件事。

    为什么这条值得专门的测试
    ──────────────────────
    重构之前，这个进程里同时存在【互不相知】的多套 Agent：
        graph.py      模块级构造 4 个 Agent
        supervisor.py __init__ 里再构造 4 个 Agent

    由此产生的 bug 是真实且静默的，不是"代码不好看"：一个端点把某 Agent
    打到熔断，另一个端点完全不知情，继续往注定失败的下游上打。

    （当时还有第三个分裂源：A/B 引擎 —— 实验结论记进 main 的引擎，而
    推荐链路读的是另一个。它已随 A/B 引擎整体移除。）

    这类问题不会让任何测试变红，只会让线上行为难以解释。所以要显式断言。
"""

from __future__ import annotations

import pytest

from harness import deps


@pytest.fixture(autouse=True)
def _clean_deps():
    deps.reset_deps()
    yield
    deps.reset_deps()


def test_agents_are_singletons() -> None:
    assert deps.get_agents() is deps.get_agents()
    assert deps.get_metrics_collector() is deps.get_metrics_collector()
    assert deps.get_supervisor() is deps.get_supervisor()


def test_supervisor_uses_shared_agents() -> None:
    """Supervisor 持有的 Agent 必须就是组合根里那一份。"""
    shared = deps.get_agents()
    supervisor = deps.get_supervisor()

    assert supervisor.user_profile_agent is shared["user_profile"]
    assert supervisor.product_rec_agent is shared["product_rec"]
    assert supervisor.marketing_copy_agent is shared["marketing_copy"]
    assert supervisor.inventory_agent is shared["inventory"]


def test_graph_uses_shared_agents() -> None:
    """
    graph.py 原先在【模块导入时】就构造了自己的一套。

    现在它必须惰性走组合根 —— 否则 /recommend 和 /recommend/graph
    两条路径的熔断状态仍然互不相知。
    """
    import inspect

    from orchestrator import graph

    src = inspect.getsource(graph)
    assert "get_agents()" in src, "graph.py 没有走组合根"
    # 模块级不该再出现裸构造
    assert "UserProfileAgent()" not in src


def test_reset_deps_gives_fresh_instances() -> None:
    first = deps.get_agents()
    deps.reset_deps()
    second = deps.get_agents()
    assert first is not second, "reset 之后必须重建，否则测试之间会互相污染"


def test_breakers_are_shared_across_paths() -> None:
    """
    共享 Agent 实例 = 共享熔断状态。
    这条是 M2 里"熔断器按名索引到共享运行时"那个决策的端到端确认。
    """
    from harness import get_runtime

    agents = deps.get_agents()
    runtime = get_runtime()

    # 用两个不同名字的 Agent 名各取一次熔断器，验证是同一份
    b1 = runtime.breaker_for("inventory")
    b2 = runtime.breaker_for("inventory")
    assert b1 is b2
    assert b1 is runtime.breaker_for(agents["inventory"].name)


def test_feature_store_follows_the_switch() -> None:
    """开关关闭时返回 None —— 连 redis 客户端都不构造（零新增失败面）。"""
    from config import get_settings

    get_settings().feature_store_enabled = False
    assert deps.get_feature_store() is None


def test_feature_store_is_a_singleton_and_reset_rebuilds_it() -> None:
    """
    ⚠️ 这条同时守着 `reset_deps()` 里那行 `get_feature_store.cache_clear()`。

    漏了它会怎样：既有测试用 `monkeypatch.setattr(get_settings(), ...)`
    【原地改】那个被 `@lru_cache` 缓存的 Settings 单例，而项目里没有
    `reset_settings` —— 于是"测试 A 开了开关建好 store、测试 B 关了开关
    却拿到 A 留下的旧 store"是必然发生的，症状是"单独跑过、一起跑挂"。
    （conftest 的 `_isolate_agent_runtime` 注释里描述过同一类问题。）
    """
    from config import get_settings

    get_settings().feature_store_enabled = True
    try:
        assert deps.get_feature_store() is deps.get_feature_store()

        deps.reset_deps()

        rebuilt = deps.get_feature_store()
        assert rebuilt is not None, "reset 之后必须重建"
    finally:
        get_settings().feature_store_enabled = False
        deps.reset_deps()
