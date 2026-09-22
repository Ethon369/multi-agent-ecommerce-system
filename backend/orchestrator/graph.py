"""
LangGraph state graph for the multi-agent recommendation pipeline.

Visualises the DAG of agent execution:

  [start] -> fan_out -> {user_profile, product_recall}  (parallel)
          -> merge_phase1 -> {rerank, inventory}         (parallel)
          -> merge_phase2 -> marketing_copy
          -> aggregate -> [end]
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from harness import current_request_id, new_request_id, request_context
from harness.deps import get_agents, get_pricing
from harness.usage import usage_scope
from models.schemas import (
    Product,
    RecommendationRequest,
    RecommendationResponse,
    UserProfile,
)

from .reporting import build_harness_report


class PipelineState(TypedDict, total=False):
    request_id: str
    user_id: str
    scene: str
    num_items: int
    context: dict[str, Any]

    user_profile: UserProfile | None
    raw_products: list[Product]
    ranked_products: list[Product]
    available_ids: set[str]
    final_products: list[Product]
    marketing_copies: list[dict[str, str]]

    agent_results: dict[str, Any]
    total_latency_ms: float
    _start_time: float


# 原先这里在【模块导入时】构造了 4 个 Agent，于是和 supervisor.py 各自
# 持有互不相知的实例：熔断状态各算各的（一个端点打挂的 Agent，另一个
# 端点不知情）。现在统一走 harness.deps，全进程一份。
#
# 惰性获取而不是在模块级构造，还有一个实际好处：import 本模块不再立刻
# 建 LLM 客户端、读 .env —— MCP Server 进程和测试进程未必需要全部依赖。


async def init_node(state: PipelineState) -> PipelineState:
    # setdefault 而不是直接赋值：路由层已经在上下文里绑好了一个 request_id，
    # 直接覆盖会让 REST 层和 graph 层的日志关联不上（同一个请求两个 id）。
    state.setdefault("request_id", new_request_id())
    state["_start_time"] = time.perf_counter()
    state["agent_results"] = {}
    return state


async def user_profile_node(state: PipelineState) -> PipelineState:
    result = await get_agents()["user_profile"].run(
        user_id=state["user_id"],
        context=state.get("context", {}),
    )
    state["user_profile"] = getattr(result, "profile", None)
    state["agent_results"]["user_profile"] = result
    return state


async def product_recall_node(state: PipelineState) -> PipelineState:
    result = await get_agents()["product_rec"].run(
        user_profile=None,
        num_items=state.get("num_items", 10) * 2,
    )
    state["raw_products"] = getattr(result, "products", [])
    state["agent_results"]["product_recall"] = result
    return state


async def parallel_phase1(state: PipelineState) -> PipelineState:
    """Run user_profile and product_recall in parallel."""
    profile_state, recall_state = await asyncio.gather(
        user_profile_node(dict(state)),
        product_recall_node(dict(state)),
    )
    state.update(profile_state)
    state.update(recall_state)
    return state


async def rerank_node(state: PipelineState) -> PipelineState:
    # 与 supervisor.py 同理：必须复用 Phase 1 召回、且库存已检查过的候选集。
    # 否则重排挑中的商品可能不在检查过的集合里，被 filter_node 静默刷掉。
    result = await get_agents()["product_rec"].run(
        user_profile=state.get("user_profile"),
        num_items=state.get("num_items", 10),
        candidates=state.get("raw_products", []),
    )
    state["ranked_products"] = getattr(result, "products", state.get("raw_products", []))
    state["agent_results"]["rerank"] = result
    return state


async def inventory_node(state: PipelineState) -> PipelineState:
    result = await get_agents()["inventory"].run(
        products=state.get("raw_products", []),
    )
    state["available_ids"] = set(getattr(result, "available_products", []))
    state["agent_results"]["inventory"] = result
    return state


async def parallel_phase2(state: PipelineState) -> PipelineState:
    """Run rerank and inventory in parallel."""
    rerank_state, inv_state = await asyncio.gather(
        rerank_node(dict(state)),
        inventory_node(dict(state)),
    )
    state.update(rerank_state)
    state.update(inv_state)
    return state


async def filter_node(state: PipelineState) -> PipelineState:
    ranked = state.get("ranked_products", [])
    avail = state.get("available_ids", set())
    num = state.get("num_items", 10)
    final = [p for p in ranked if p.product_id in avail]
    if not final:
        final = ranked
    state["final_products"] = final[:num]
    return state


async def marketing_copy_node(state: PipelineState) -> PipelineState:
    result = await get_agents()["marketing_copy"].run(
        user_profile=state.get("user_profile"),
        products=state.get("final_products", []),
    )
    state["marketing_copies"] = getattr(result, "copies", [])
    state["agent_results"]["marketing_copy"] = result
    return state


async def aggregate_node(state: PipelineState) -> PipelineState:
    state["total_latency_ms"] = (time.perf_counter() - state.get("_start_time", 0)) * 1000
    return state


def build_recommendation_graph() -> StateGraph:
    """Build and compile the LangGraph state graph."""
    graph = StateGraph(PipelineState)

    graph.add_node("init", init_node)
    graph.add_node("parallel_phase1", parallel_phase1)
    graph.add_node("parallel_phase2", parallel_phase2)
    graph.add_node("filter", filter_node)
    graph.add_node("marketing_copy", marketing_copy_node)
    graph.add_node("aggregate", aggregate_node)

    graph.set_entry_point("init")
    graph.add_edge("init", "parallel_phase1")
    graph.add_edge("parallel_phase1", "parallel_phase2")
    graph.add_edge("parallel_phase2", "filter")
    graph.add_edge("filter", "marketing_copy")
    graph.add_edge("marketing_copy", "aggregate")
    graph.add_edge("aggregate", END)

    return graph.compile()


# ── 响应组装：与 /api/v1/recommend 保持【同一个契约】 ──────────────
#
# 在这之前，这个端点返回的是一个【缩小版】对象（只有 products /
# marketing_copies / request_id / user_id / total_latency_ms），
# 而且没有 response_model —— 于是：
#   1. OpenAPI 里没有 schema，前端生成不了类型；
#   2. 前端要为同一个"推荐"概念写两套解析；
#   3. agent_results 与 harness 整个丢了 —— 也就是这个项目
#      最值钱的那部分（各 Agent 耗时、熔断状态、token 账本）在
#      这条路径上完全不可见。
#   4. 出错时返回 {"error": "..."} 但状态码是 200 —— 客户端按
#      status code 判断成败时会认为它成功了。
#
# 现在两条路径产出完全相同的结构。附带的好处：可以把两套编排器
# （asyncio.gather 版 vs LangGraph 版）的输出直接对比，这才让
# "同时保留两套实现"这件事有了意义。


def build_response(
    state: PipelineState, usage_report: dict[str, Any]
) -> RecommendationResponse:
    """把图跑完后的 state 组装成 RecommendationResponse。"""
    raw: dict[str, Any] = dict(state.get("agent_results") or {})

    # 键名归一化。图内部把两次 product_rec 调用分别记成
    # product_recall（Phase 1 首次召回）与 rerank（Phase 2 重排），
    # 而响应契约里只有一个 product_rec —— 这个口径与 supervisor 一致：
    # 那边传出去的也是【重排后】的结果，首次召回是中间产物、不进响应。
    #
    # 不做归一化的话，前端会看到 5 个键里夹着一个它不认识的 product_recall，
    # 而少的那个 product_rec 又恰好是它真正需要的。
    mapped = {
        "user_profile": raw.get("user_profile"),
        "product_rec": raw.get("rerank"),
        "marketing_copy": raw.get("marketing_copy"),
        "inventory": raw.get("inventory"),
    }

    # 丢掉 None，而不是把它塞进 dict[str, AgentResult]。
    # 某个节点没跑完时，塞 None 会让 pydantic 在响应模型校验阶段直接报错 ——
    # 而这里正确的语义是"少一个键"，不是"整个请求失败"。
    results = {k: v for k, v in mapped.items() if v is not None}

    return RecommendationResponse(
        request_id=str(state.get("request_id") or new_request_id()),
        user_id=str(state.get("user_id") or ""),
        products=list(state.get("final_products") or []),
        marketing_copies=list(state.get("marketing_copies") or []),
        agent_results=results,
        harness=build_harness_report(results, usage_report),
        total_latency_ms=float(state.get("total_latency_ms") or 0.0),
    )


async def run_recommendation_graph(
    graph: Any, request: RecommendationRequest
) -> RecommendationResponse:
    """
    跑一次图流水线并产出统一响应。

    放在这里而不是路由里：让"图怎么跑、state 怎么变成响应"与 HTTP 层解耦，
    路由只负责解析请求、判空、返回。
    """
    # 复用接入层已绑好的 id（没有才新建），理由同 supervisor.recommend。
    request_id = current_request_id() or new_request_id()
    state: PipelineState = {
        "request_id": request_id,
        "user_id": request.user_id,
        "scene": request.scene,
        "num_items": request.num_items,
        "context": request.context,
    }

    # 两个 with 都必须在 ainvoke 【之前】进入，理由见 supervisor.py：
    # 上下文是在子任务【创建时】复制的，事后再绑就晚了。
    # 账本尤其致命 —— 子任务会拿到 None，token 数会静默丢失。
    with (
        request_context(request_id, user_id=request.user_id, scene=request.scene),
        usage_scope() as usage,
    ):
        result = await graph.ainvoke(state)

    return build_response(result, usage.as_report(get_pricing()))
