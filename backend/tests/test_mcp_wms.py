"""
WMS MCP Server 测试。

全部用【进程内连接】（`Client(server)`）—— 不起子进程、不占端口、不依赖
stdio 时序。这是 mcp v2 相对 v1 的一个实质改进（v1 需要
`create_connected_server_and_client_session()`，v2 已移除）。

对应 PRD 的验收项：
    A2  工具逻辑正确性（本文件）
    A5  WMS 库存 != Product.stock —— 用来【证明】推荐链路真的走了 MCP，
        而不是"看起来一样所以大概是通的"

每个测试用独立的临时数据库，互不污染。
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from mcp import Client

from mcp_servers import init_wms_db
from mcp_servers.wms_server import server


@pytest.fixture
def wms_db(tmp_path, monkeypatch):
    """建一个临时 WMS 库，并把服务指向它。"""
    db_path = str(tmp_path / "wms_test.db")
    monkeypatch.setenv("ECOM_MCP_WMS_DB_PATH", db_path)
    stats = init_wms_db.init_db(db_path, reset=True)
    assert stats["products"] == 15
    assert stats["diverged_from_product_stock"] > 0, "种子必须与目录库存有差异，否则 A5 无从证明"
    return db_path


@pytest.mark.anyio
async def test_lists_all_four_tools(wms_db) -> None:
    async with Client(server) as client:
        tools = await client.list_tools()

    names = {t.name for t in tools.tools}
    assert names == {"query_stock", "batch_query_stock", "list_low_stock", "upsert_stock"}

    for t in tools.tools:
        assert t.description, f"{t.name} 缺 description —— 模型靠它判断该不该调用"
        assert t.input_schema.get("type") == "object"


@pytest.mark.anyio
async def test_query_stock_returns_wms_data(wms_db) -> None:
    async with Client(server) as client:
        r = await client.call_tool("query_stock", {"product_id": "P003"})

    assert r.is_error is False
    data = r.structured_content
    assert data["found"] is True
    # P003 在种子规则里是 n%3==0 -> 目录库存 + 137
    assert data["stock"] == 1000 + 137
    assert data["level"] in {"critical", "warning", "normal"}


@pytest.mark.anyio
async def test_query_stock_unknown_id_is_not_an_exception(wms_db) -> None:
    """查不到要返回可判定的结果，而不是抛异常 —— 让调用方能自行处置。"""
    async with Client(server) as client:
        r = await client.call_tool("query_stock", {"product_id": "P999"})

    assert r.is_error is False
    assert r.structured_content["found"] is False
    assert r.structured_content["error"] == "product_not_found"


@pytest.mark.anyio
async def test_batch_query_distinguishes_zero_from_missing(wms_db) -> None:
    """
    P007 在 WMS 缺货（0），P999 根本不在库里。

    两者必须可区分：返回 -1 而不是省略键，否则调用方无法判断
    "这个商品缺货"和"这个商品没有数据"。返回 -1 比返回 0 更明确。
    """
    async with Client(server) as client:
        r = await client.call_tool(
            "batch_query_stock", {"product_ids": ["P001", "P007", "P999"]}
        )

    data = r.structured_content
    assert data["P001"] == 500
    assert data["P007"] == 0, "WMS 里 P007 是缺货"
    assert data["P999"] == -1, "不在库里的商品必须与缺货区分开"


@pytest.mark.anyio
async def test_batch_query_empty_input(wms_db) -> None:
    async with Client(server) as client:
        r = await client.call_tool("batch_query_stock", {"product_ids": []})
    assert r.structured_content == {}


@pytest.mark.anyio
async def test_list_low_stock_levels(wms_db) -> None:
    """
    注意 MCP 的结构化输出语义：工具返回【裸数组】时，structured_content
    会被包成 {"result": [...]}，因为 MCP 要求结构化输出的顶层是 JSON 对象。
    返回 dict 的工具（query_stock / batch_query_stock）则是原样返回。

    这条对后续的运营 Copilot 有直接影响 —— 模型看到的是 {"result": [...]}，
    不是裸数组。harness 的工具层需要统一处理这个差异。
    """
    async with Client(server) as client:
        r = await client.call_tool("list_low_stock", {})

    rows = r.structured_content["result"]
    assert isinstance(rows, list) and rows
    assert [row["stock"] for row in rows] == sorted(row["stock"] for row in rows), (
        "必须按库存升序，方便直接看最紧急的"
    )
    for row in rows:
        assert row["level"] in {"critical", "warning"}
        assert row["action"] in {"urgent_restock", "plan_restock"}


@pytest.mark.anyio
async def test_structured_content_shape_by_return_type(wms_db) -> None:
    """
    把上面那条语义固化下来，避免以后有人"顺手"改返回类型时踩坑。
        -> dict   : structured_content 就是那个 dict
        -> list   : structured_content 是 {"result": [...]}
    """
    async with Client(server) as client:
        dict_r = await client.call_tool("query_stock", {"product_id": "P003"})
        list_r = await client.call_tool("list_low_stock", {})

    assert "product_id" in dict_r.structured_content, "dict 返回应原样透出"
    assert set(list_r.structured_content.keys()) == {"result"}, (
        "list 返回会被 MCP 包一层 result"
    )


@pytest.mark.anyio
async def test_upsert_stock_roundtrip(wms_db) -> None:
    async with Client(server) as client:
        w = await client.call_tool("upsert_stock", {"product_id": "P001", "stock": 7})
        assert w.structured_content["ok"] is True

        r = await client.call_tool("query_stock", {"product_id": "P001"})

    assert r.structured_content["stock"] == 7
    assert r.structured_content["level"] == "critical", "7 <= 安全库存 50，应为 critical"


@pytest.mark.anyio
async def test_upsert_rejects_negative(wms_db) -> None:
    async with Client(server) as client:
        r = await client.call_tool("upsert_stock", {"product_id": "P001", "stock": -5})
    assert r.structured_content["ok"] is False
    assert r.structured_content["error"] == "stock_negative"


@pytest.mark.anyio
async def test_resource_readable(wms_db) -> None:
    """MCP 的第二种能力类型：资源（Host 侧可以"读"，不只是"调用"）。"""
    async with Client(server) as client:
        r = await client.read_resource("wms://stock/P003")

    payload = json.loads(r.contents[0].text)
    assert payload["product_id"] == "P003"
    assert payload["stock"] == 1137


# ── A5：证明"真的走了 MCP"而不是"看起来一样" ──────────────

def test_a5_wms_differs_from_product_catalog(wms_db) -> None:
    """
    这是整个 MCP 接入能被【证明】的根据。

    若 WMS 库存恰好等于 Product.stock，那么 MCP 通与不通的输出完全相同 ——
    测试全绿也说明不了任何事。种子数据必须制造可见差异。
    """
    from agents.product_rec_agent import MOCK_PRODUCTS

    conn = sqlite3.connect(wms_db)
    try:
        wms = {r[0]: r[1] for r in conn.execute("SELECT product_id, stock FROM wms_stock")}
    finally:
        conn.close()

    catalog = {p.product_id: p.stock for p in MOCK_PRODUCTS}

    diverged = [pid for pid in catalog if wms[pid] != catalog[pid]]
    assert len(diverged) >= 5, f"差异商品太少，A5 说服力不足: {diverged}"

    # 必须存在"目录有货但 WMS 缺货"的商品 —— 这是推荐结果里可见的差异
    wms_zero_catalog_positive = [
        pid for pid in catalog if wms[pid] == 0 and catalog[pid] > 0
    ]
    assert wms_zero_catalog_positive, (
        "至少要有一个『目录说有货、WMS 说没货』的商品，"
        "否则无法在推荐结果里观察到 MCP 是否生效"
    )


def test_seed_is_deterministic(wms_db) -> None:
    """差异必须是确定性的，否则测试无法复现。"""
    first = init_wms_db.seed_stock("P007", 2000)
    for _ in range(5):
        assert init_wms_db.seed_stock("P007", 2000) == first
    assert first == 0
