"""
库存决策Agent
- 实时库存查询：通过 MCP 客户端消费 WMS Server（原为 Product.stock 假数据）
- 库存预警：安全库存阈值 + 补货建议
- 限购策略：基于库存深度 + 促销热度动态调整

    MCP 集成点只有这一个文件，且只有两处：
      1. __init__  里按开关注入 mcp 客户端
      2. _execute  开头一次性批量取库存
    其余逻辑（预警分级、限购策略）完全不动。

    为什么不按"每个商品查一次"的写法
    ────────────────────────────────
    实测每次 MCP 调用的成本是【约 1.15 秒】（短连接要重启子进程，
    而 import mcp 本身就要 1 秒 —— 见 services/mcp_client.py 的说明）。
    逐个商品查会变成 N × 1.15s：一次推荐 P1 召回最多 30 个商品，
    那就是 35 秒，比整条链路的其他部分加起来还慢。
    batch_query_stock 把这个降为 1 次 —— 这是整条 MCP 接入里最关键的
    一个接口决策。

    降级（PRD 决策 5，硬性要求）
    ────────────────────────────
    MCP 起不来 / 超时 / 返回异常时，必须回落到 Product.stock，
    主推荐接口仍然返回 200。降级必须【可观测】—— data.source 标注
    "mcp" 还是 "fallback"，否则"降级"会退化成静默的错误掩盖。
"""

from __future__ import annotations

from typing import Any

import structlog

from models.schemas import InventoryResult, Product

from .base_agent import BaseAgent

logger = structlog.get_logger()

SAFETY_STOCK_THRESHOLD = 50
LOW_STOCK_THRESHOLD = 100
HOT_ITEM_PURCHASE_LIMIT = 2

# batch_query_stock 对"不在 WMS 里"的商品返回 -1，用来区别于"缺货(0)"。
MISSING_IN_WMS = -1


class InventoryAgent(BaseAgent):
    def __init__(self):
        from config import get_settings

        settings = get_settings()
        super().__init__(
            name="inventory",
            timeout=settings.agent_timeout_inventory,
        )
        self.mcp_enabled: bool = settings.mcp_wms_enabled
        # 预留的注入点（PRD 里的 self.db）。仅在开关打开时才有值；
        # 关闭时不构造客户端，也就没有新的失败面（验收项 A7）。
        self.db: Any = None
        if self.mcp_enabled:
            from services.mcp_client import WMSMCPClient

            self.db = WMSMCPClient()

    async def _execute(self, **kwargs: Any) -> InventoryResult:
        products: list[Product] = kwargs.get("products", [])

        stock_map, source = await self._fetch_stocks(products)

        available = []
        low_stock_alerts = []
        purchase_limits: dict[str, int] = {}

        for product in products:
            stock = self._resolve_stock(product, stock_map)

            if stock <= 0:
                continue

            available.append(product.product_id)

            if stock <= SAFETY_STOCK_THRESHOLD:
                low_stock_alerts.append({
                    "product_id": product.product_id,
                    "name": product.name,
                    "current_stock": stock,
                    "level": "critical",
                    "action": "urgent_restock",
                })
            elif stock <= LOW_STOCK_THRESHOLD:
                low_stock_alerts.append({
                    "product_id": product.product_id,
                    "name": product.name,
                    "current_stock": stock,
                    "level": "warning",
                    "action": "plan_restock",
                })

            limit = self._calc_purchase_limit(product, stock)
            if limit is not None:
                purchase_limits[product.product_id] = limit

        return InventoryResult(
            success=True,
            available_products=available,
            low_stock_alerts=low_stock_alerts,
            purchase_limits=purchase_limits,
            data={
                "total_checked": len(products),
                "available_count": len(available),
                "alert_count": len(low_stock_alerts),
                # 降级必须可观测 —— 否则"降级"就是静默的错误掩盖
                "source": source,
            },
            confidence=0.95 if source == "mcp" else 0.7,
        )

    async def _fetch_stocks(
        self, products: list[Product]
    ) -> tuple[dict[str, int], str]:
        """
        一次性批量取库存。返回 (库存表, 来源标记)。

        任何失败都回落到空表 + "fallback" —— 本方法不抛异常，
        因为库存是主链路上的一环，不能把整条链路拖垮。
        """
        if not self.mcp_enabled or self.db is None or not products:
            return {}, "fallback"

        product_ids = [p.product_id for p in products]
        try:
            result = await self.db.batch_query_stock(product_ids)
        except Exception as exc:
            # 客户端本身承诺不抛，这里是最后一道防线
            logger.error("inventory.mcp_unexpected", error=str(exc)[:200])
            return {}, "fallback"

        if not result:
            logger.warning("inventory.mcp_degraded", reason="empty_or_error",
                           product_count=len(product_ids))
            return {}, "fallback"

        logger.info("inventory.stock_source", source="mcp", product_count=len(result))
        return result, "mcp"

    def _resolve_stock(self, product: Product, stock_map: dict[str, int]) -> int:
        """
        从批量结果里取这个商品的库存。

        三种情况：
          - 表里有值且有效      -> 用 WMS 的值（这是走通 MCP 的路径）
          - 值为 -1（不在 WMS） -> 回落到 Product.stock，属于数据缺失而非缺货
          - 表里没有（已降级）  -> 回落到 Product.stock
        """
        stock = stock_map.get(product.product_id)
        if stock is None or stock == MISSING_IN_WMS:
            return product.stock
        return stock

    def _calc_purchase_limit(self, product: Product, stock: int) -> int | None:
        """Dynamic purchase limit based on stock depth and product heat."""
        is_hot = "新品" in product.tags or "旗舰" in product.tags
        if stock <= SAFETY_STOCK_THRESHOLD:
            return 1
        if stock <= LOW_STOCK_THRESHOLD and is_hot:
            return HOT_ITEM_PURCHASE_LIMIT
        if is_hot and stock <= 300:
            return 3
        return None
