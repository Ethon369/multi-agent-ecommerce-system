"""
recommend_server 测试（MCP 服务端侧）。

全部用【进程内连接】`Client(server)` —— 不起子进程、不占端口。

对应 PRD 的验收项：
    A1  工具可被发现（list_tools 能列出全部 2 个）
    A2  工具逻辑正确

不测 recommend_products 的完整链路（那会真调 LLM，~10 秒且花钱）——
单独用一条标记测试覆盖，默认跳过。
"""

from __future__ import annotations

import pytest
from mcp import Client

from mcp_servers.recommend_server import server

EXPECTED_TOOLS = {
    "recommend_products",
    "get_metrics",
}


@pytest.mark.anyio
async def test_all_tools_discoverable() -> None:
    """A1：工具必须能被发现，且每个都要有 description。"""
    async with Client(server) as client:
        tools = await client.list_tools()

    names = {t.name for t in tools.tools}
    assert names == EXPECTED_TOOLS

    for t in tools.tools:
        assert t.description, f"{t.name} 缺 description —— 模型靠它判断该不该调"
        assert t.input_schema.get("type") == "object"


@pytest.mark.anyio
async def test_recommend_products_documents_its_slowness() -> None:
    """
    recommend_products 会真调大模型（约 5-15 秒）。

    工具描述里【必须】写明这点，否则 Host 侧的模型会按本地查询的预期
    给它一个短超时，然后判定失败。
    """
    async with Client(server) as client:
        tools = await client.list_tools()

    desc = next(t.description for t in tools.tools if t.name == "recommend_products")
    assert "秒" in desc, "必须告诉调用方这个工具是慢的"


@pytest.mark.anyio
async def test_get_metrics() -> None:
    async with Client(server) as client:
        r = await client.call_tool("get_metrics", {})

    assert r.is_error is False
    data = r.structured_content
    assert {"agents", "llm", "breakers"} <= set(data.keys())


# ── 参数校验（不真调 LLM，因为校验在调用之前）──────────────

@pytest.mark.anyio
@pytest.mark.parametrize("num_items,expected_error", [
    (0, "num_items_must_be_positive"),
    (-1, "num_items_must_be_positive"),
    (99, "num_items_too_large"),
])
async def test_num_items_bounds(num_items: int, expected_error: str) -> None:
    """
    边界校验必须在【调 LLM 之前】完成 —— 否则一个离谱的参数会先烧掉一次
    完整的流水线（约 10 秒 + 真金白银）才被拒绝。
    """
    async with Client(server) as client:
        r = await client.call_tool(
            "recommend_products", {"user_id": "u_x", "num_items": num_items}
        )

    assert r.is_error is False
    assert r.structured_content["ok"] is False
    assert r.structured_content["error"] == expected_error


# ── 完整链路（真调 LLM，默认跳过）────────────────────────────

@pytest.mark.anyio
@pytest.mark.skipif(
    not __import__("os").environ.get("RUN_SLOW_MCP_TESTS"),
    reason="会真调大模型（约 10 秒 + 产生费用）。设 RUN_SLOW_MCP_TESTS=1 才跑",
)
async def test_recommend_products_full_path() -> None:
    """
    完整链路。重点验证【扁平结构绕开了 pydantic 截断】——
    这是 main.py 的 /recommend/graph 端点采用同样做法的原因。
    """
    async with Client(server) as client:
        r = await client.call_tool(
            "recommend_products", {"user_id": "u_test", "num_items": 3}
        )

    assert r.is_error is False
    d = r.structured_content

    # 顶层必须能直接看到商品和文案 —— 而不是被埋在 agent_results 里
    assert d["products"], "商品列表为空"
    assert len(d["products"]) == 3, "阶段 A 修过这个 bug，不该退化"
    assert d["marketing_copies"]
    assert d["request_id"]
    assert d["usage"] is not None, "用量报告要能透出来"
