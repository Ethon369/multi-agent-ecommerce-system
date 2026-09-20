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

from harness import get_runtime, new_request_id, request_context
from harness.deps import get_ab_engine, get_agents, get_pricing
from harness.usage import usage_scope
from models.schemas import (
    HarnessReport,
    HarnessUsageReport,
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
        # 关键：这两个 with 都必须在下面每一个 asyncio.gather 【之前】进入 ——
        # asyncio 任务在【创建时】复制上下文，所以 gather 之前绑定的
        # request_id 和账本会被子任务自动继承；事后再绑就晚了
        # （账本尤其致命：子任务会拿到 None，token 数静默丢失）。
        with (
            request_context(request_id, user_id=request.user_id, scene=request.scene),
            usage_scope() as usage,
        ):
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
            #
            # 关键：把 Phase 1 召回的那批商品【原样传给重排】。
            # 库存 Agent 检查的就是 raw_products，所以重排必须在同一个集合里挑，
            # 否则重排挑中的商品不在检查过的集合里，会被下面的过滤【静默刷掉】，
            # 最终返回数量少于 num_items（实测要 5 个稳定只给 2-3 个）。
            # 顺带也去掉了一次多余的重复召回。
            rerank_task = self.product_rec_agent.run(
                user_profile=user_profile,
                num_items=request.num_items,
                candidates=raw_products,
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

        results = {
            "user_profile": profile_result,
            "product_rec": rerank_result,
            "marketing_copy": copy_result,
            "inventory": inventory_result,
        }
        usage_report = usage.as_report(get_pricing())

        logger.info(
            "supervisor.complete",
            request_id=request_id,
            total_latency_ms=round(total_latency, 1),
            product_count=len(final_products),
            copy_count=len(copies),
            # 把成本和 token 数打在完成事件上 —— 这样 grep 一次 request_id
            # 就能同时回答"这次请求花了多久"和"花了多少钱"。
            llm_calls=usage_report["llm_calls"],
            input_tokens=usage_report["input_tokens"],
            output_tokens=usage_report["output_tokens"],
            reasoning_tokens=usage_report["reasoning_tokens"],
            cost_usd=usage_report["cost_usd"],
        )

        return RecommendationResponse(
            request_id=request_id,
            user_id=request.user_id,
            products=final_products,
            marketing_copies=copies,
            experiment_group=experiment.get("group", "control"),
            agent_results=results,
            harness=self._harness_report(request_id, results, usage_report),
            total_latency_ms=total_latency,
        )

    @staticmethod
    def _harness_report(
        request_id: str,
        results: dict[str, Any],
        usage_report: dict[str, Any],
    ) -> HarnessReport:
        """把运行时保障层的状态汇总成一份自述报告。"""
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
