"""
Supervisor编排器 — 并行分发 + 聚合模式

                    ┌──────────────┐
                    │  Supervisor   │
                    └──────┬───────┘
           ┌───────┬───────┼───────┬────────┐
           ▼       ▼       ▼       ▼        │
      UserProfile  ProdRec  MktCopy  Inventory │
           │       │       │       │        │
           └───────┴───────┴───────┘        │
                    │                        │
                    ▼                        │
               Aggregator ◄─────────────────┘
                    │
                    ▼
              A/B Test Engine
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

from harness import new_request_id, request_context
from harness.deps import get_ab_engine, get_agents
from models.schemas import (
    Product,
    RecommendationRequest,
    RecommendationResponse,
    UserProfile,
)
from services.ab_test import ABTestEngine

logger = structlog.get_logger()


class SupervisorOrchestrator:
    """Coordinates four agents in parallel-then-aggregate pattern."""

    def __init__(self, ab_engine: ABTestEngine | None = None):
        # 走 harness.deps 拿共享单例，而不是各自 new 一套。
        # 否则 graph.py 那条路径会持有另一组 Agent 和另一个 ABTestEngine，
        # 导致熔断状态与 A/B 实验结果两边互不相知。
        agents = get_agents()
        self.user_profile_agent = agents["user_profile"]
        self.product_rec_agent = agents["product_rec"]
        self.marketing_copy_agent = agents["marketing_copy"]
        self.inventory_agent = agents["inventory"]
        self.ab_engine = ab_engine or get_ab_engine()

    async def recommend(self, request: RecommendationRequest) -> RecommendationResponse:
        request_id = new_request_id()
        start = time.perf_counter()

        logger.info(
            "supervisor.start",
            request_id=request_id,
            user_id=request.user_id,
            scene=request.scene,
        )

        # 整个流水线跑在请求上下文里。
        # 关键：这个 with 必须在下面每一个 asyncio.gather 【之前】进入 ——
        # asyncio 任务在【创建时】复制上下文，所以在 gather 之前绑定的
        # request_id 会被两个 Phase 的子任务自动继承；事后再绑就晚了。
        with request_context(request_id, user_id=request.user_id, scene=request.scene):
            experiment = self.ab_engine.assign(request.user_id)

            # Phase 1: parallel — user profile + product recall
            profile_result, rec_result = await asyncio.gather(
                self.user_profile_agent.run(
                    user_id=request.user_id,
                    context=request.context,
                ),
                self.product_rec_agent.run(
                    user_profile=None,
                    num_items=request.num_items * 2,
                ),
            )

            user_profile: UserProfile | None = getattr(profile_result, "profile", None)
            raw_products: list[Product] = getattr(rec_result, "products", [])

            # Phase 2: parallel — re-rank with profile + inventory check + copy generation
            rerank_task = self.product_rec_agent.run(
                user_profile=user_profile,
                num_items=request.num_items,
            )
            inventory_task = self.inventory_agent.run(products=raw_products)

            rerank_result, inventory_result = await asyncio.gather(
                rerank_task, inventory_task
            )

            ranked_products: list[Product] = getattr(
                rerank_result, "products", raw_products
            )

            available_ids = set(getattr(inventory_result, "available_products", []))
            final_products = [p for p in ranked_products if p.product_id in available_ids]
            if not final_products:
                final_products = ranked_products[:request.num_items]
            final_products = final_products[:request.num_items]

            # Phase 3: marketing copy generation with final product list
            copy_result = await self.marketing_copy_agent.run(
                user_profile=user_profile,
                products=final_products,
            )
            copies = getattr(copy_result, "copies", [])

        total_latency = (time.perf_counter() - start) * 1000

        logger.info(
            "supervisor.complete",
            request_id=request_id,
            total_latency_ms=round(total_latency, 1),
            product_count=len(final_products),
            copy_count=len(copies),
        )

        return RecommendationResponse(
            request_id=request_id,
            user_id=request.user_id,
            products=final_products,
            marketing_copies=copies,
            experiment_group=experiment.get("group", "control"),
            agent_results={
                "user_profile": profile_result,
                "product_rec": rerank_result,
                "marketing_copy": copy_result,
                "inventory": inventory_result,
            },
            total_latency_ms=total_latency,
        )
