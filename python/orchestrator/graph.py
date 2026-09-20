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

from agents import (
    InventoryAgent,
    MarketingCopyAgent,
    ProductRecAgent,
    UserProfileAgent,
)
from harness import new_request_id
from models.schemas import Product, UserProfile
from services.ab_test import ABTestEngine


class PipelineState(TypedDict, total=False):
    request_id: str
    user_id: str
    scene: str
    num_items: int
    context: dict[str, Any]
    experiment_group: str

    user_profile: UserProfile | None
    raw_products: list[Product]
    ranked_products: list[Product]
    available_ids: set[str]
    final_products: list[Product]
    marketing_copies: list[dict[str, str]]

    agent_results: dict[str, Any]
    total_latency_ms: float
    _start_time: float


user_profile_agent = UserProfileAgent()
product_rec_agent = ProductRecAgent()
marketing_copy_agent = MarketingCopyAgent()
inventory_agent = InventoryAgent()
ab_engine = ABTestEngine()


async def init_node(state: PipelineState) -> PipelineState:
    # setdefault 而不是直接赋值：路由层已经在上下文里绑好了一个 request_id，
    # 直接覆盖会让 REST 层和 graph 层的日志关联不上（同一个请求两个 id）。
    state.setdefault("request_id", new_request_id())
    state["_start_time"] = time.perf_counter()
    state["agent_results"] = {}
    exp = ab_engine.assign(state["user_id"])
    state["experiment_group"] = exp.get("group", "control")
    return state


async def user_profile_node(state: PipelineState) -> PipelineState:
    result = await user_profile_agent.run(
        user_id=state["user_id"],
        context=state.get("context", {}),
    )
    state["user_profile"] = getattr(result, "profile", None)
    state["agent_results"]["user_profile"] = result
    return state


async def product_recall_node(state: PipelineState) -> PipelineState:
    result = await product_rec_agent.run(
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
    result = await product_rec_agent.run(
        user_profile=state.get("user_profile"),
        num_items=state.get("num_items", 10),
    )
    state["ranked_products"] = getattr(result, "products", state.get("raw_products", []))
    state["agent_results"]["rerank"] = result
    return state


async def inventory_node(state: PipelineState) -> PipelineState:
    result = await inventory_agent.run(
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
    result = await marketing_copy_agent.run(
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
