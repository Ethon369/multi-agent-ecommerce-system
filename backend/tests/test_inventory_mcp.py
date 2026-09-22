"""
InventoryAgent 的 MCP 接线测试 —— 全部离线，用假客户端，不起子进程。

对应 PRD 的验收项：
    A4  降级：MCP 不可用时回落到 Product.stock，且仍然成功
    A7  开关关闭时零影响
以及一条本项目实测得出的关键约束：
    无论多少个商品，只能发起【一次】MCP 调用。
    实测每次 MCP 调用约 1.15 秒（短连接要重启子进程，import mcp 就占 1 秒），
    逐个商品查会变成 N × 1.15s —— 一次推荐召回 30 个就是 35 秒。
"""

from __future__ import annotations

from typing import Any

import pytest

from agents.inventory_agent import InventoryAgent
from agents.product_rec_agent import MOCK_PRODUCTS


class FakeMCPClient:
    """假的 WMS 客户端。记录调用次数，便于断言"只查了一次"。"""

    def __init__(self, result: dict[str, int] | None = None):
        self.result = result
        self.calls: list[list[str]] = []

    async def batch_query_stock(self, product_ids: list[str]) -> dict[str, int] | None:
        self.calls.append(list(product_ids))
        return self.result


def make_agent(monkeypatch, *, enabled: bool, client: Any = None) -> InventoryAgent:
    from config import get_settings

    monkeypatch.setattr(get_settings(), "mcp_wms_enabled", enabled)
    agent = InventoryAgent()
    if client is not None:
        agent.db = client
    return agent


@pytest.mark.anyio
async def test_mcp_disabled_uses_product_stock(monkeypatch) -> None:
    """A7：开关关闭时零影响 —— 行为和集成 MCP 之前完全一致。"""
    agent = make_agent(monkeypatch, enabled=False)
    assert agent.db is None, "开关关闭时不该构造客户端（也就没有新的失败面）"

    result = await agent.run(products=list(MOCK_PRODUCTS))

    assert result.success is True
    assert result.data["source"] == "fallback"
    assert len(result.available_products) == 15, "所有商品的 Product.stock 都 > 0"


@pytest.mark.anyio
async def test_mcp_enabled_uses_wms_stock(monkeypatch) -> None:
    """走通路径：库存来自 WMS，缺货商品被过滤掉。"""
    fake = FakeMCPClient({p.product_id: 100 for p in MOCK_PRODUCTS})
    fake.result["P007"] = 0    # WMS 判定缺货
    fake.result["P014"] = 0
    agent = make_agent(monkeypatch, enabled=True, client=fake)

    result = await agent.run(products=list(MOCK_PRODUCTS))

    assert result.data["source"] == "mcp"
    assert result.confidence == 0.95, "走通 MCP 时置信度更高"
    assert "P007" not in result.available_products
    assert "P014" not in result.available_products
    assert len(result.available_products) == 13


@pytest.mark.anyio
async def test_a4_falls_back_when_server_unavailable(monkeypatch) -> None:
    """
    A4 —— 本方案的灵魂。

    MCP 返回 None（服务挂了 / 超时 / 报错）时必须回落到 Product.stock，
    仍然成功。只做"能用"是 A3，能"挂掉也不怕"才是 A4。
    """
    fake = FakeMCPClient(result=None)   # 客户端失败时的返回值
    agent = make_agent(monkeypatch, enabled=True, client=fake)

    result = await agent.run(products=list(MOCK_PRODUCTS))

    assert result.success is True, "MCP 挂掉不该让库存 Agent 失败"
    assert result.data["source"] == "fallback", "降级必须可观测"
    assert len(result.available_products) == 15, "回落到了 Product.stock"


@pytest.mark.anyio
async def test_a4_falls_back_when_client_raises(monkeypatch) -> None:
    """客户端承诺不抛，但这里是最后一道防线 —— 抛了也必须在 Agent 层被兜住。"""
    class ExplodingClient:
        async def batch_query_stock(self, product_ids):
            raise RuntimeError("boom")

    agent = make_agent(monkeypatch, enabled=True, client=ExplodingClient())

    result = await agent.run(products=list(MOCK_PRODUCTS))

    assert result.success is True
    assert result.data["source"] == "fallback"


@pytest.mark.anyio
async def test_missing_in_wms_falls_back_per_product(monkeypatch) -> None:
    """
    batch_query_stock 对"不在 WMS 里"的商品返回 -1。

    -1 是【数据缺失】，不是【缺货】—— 必须回落到 Product.stock，
    而不能当成 0 把商品过滤掉。这两者混淆会导致商品凭空消失。
    """
    fake = FakeMCPClient({"P001": 100, "P002": -1})   # P002 不在 WMS
    agent = make_agent(monkeypatch, enabled=True, client=fake)

    result = await agent.run(products=list(MOCK_PRODUCTS))

    assert "P001" in result.available_products
    assert "P002" in result.available_products, (
        "-1 被当成缺货了 —— 商品会因为数据缺失而消失"
    )


@pytest.mark.anyio
async def test_exactly_one_mcp_call_regardless_of_product_count(monkeypatch) -> None:
    """
    这条约束是实测逼出来的，不是设计偏好。

    实测每次 MCP 调用约 1.15 秒（短连接重启子进程，import mcp 占约 1 秒）。
    逐个商品查会变成 N × 1.15s：一次推荐 P1 召回 30 个商品就是 35 秒，
    比整条链路的其他部分加起来还慢。
    """
    fake = FakeMCPClient({p.product_id: 100 for p in MOCK_PRODUCTS})
    agent = make_agent(monkeypatch, enabled=True, client=fake)

    await agent.run(products=list(MOCK_PRODUCTS))

    assert len(fake.calls) == 1, f"发起了 {len(fake.calls)} 次 MCP 调用，应该只有 1 次"
    assert len(fake.calls[0]) == 15, "一次调用要带上全部商品 ID"


@pytest.mark.anyio
async def test_no_mcp_call_when_product_list_empty(monkeypatch) -> None:
    """空商品列表不该白白起一次子进程（1.15 秒）。"""
    fake = FakeMCPClient({})
    agent = make_agent(monkeypatch, enabled=True, client=fake)

    result = await agent.run(products=[])

    assert fake.calls == [], "没有商品还去调 MCP，白白花 1.15 秒"
    assert result.success is True
