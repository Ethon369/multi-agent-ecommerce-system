"""响应模型的序列化契约。

守着实测踩过的一个坑：`agent_results` 声明成基类 `AgentResult` 时，
pydantic 按【声明类型】序列化，子类独有字段会被【静默截断】——
不报错、无日志，HTTP 响应里就是少了。

实测记录：`ProductRecResult` 有 8 个字段，经过 `RecommendationResponse`
序列化后只剩 6 个（`products` / `recall_strategy` 消失）。
"""

from __future__ import annotations

from models.schemas import (
    AgentResult,
    InventoryResult,
    MarketingCopyResult,
    Product,
    ProductRecResult,
    RecommendationResponse,
    UserProfileResult,
)


def _response() -> RecommendationResponse:
    return RecommendationResponse(request_id="req1", user_id="u1")


def test_subclass_fields_survive_serialization() -> None:
    """
    四个子类各自的独有字段都必须出现在序列化结果里。

    断言的是【字段存在】，不是值非空 —— 被截断时是连字段都没有。
    """
    r = _response()
    r.agent_results["user_profile"] = UserProfileResult()
    r.agent_results["product_rec"] = ProductRecResult(
        products=[
            Product(product_id="P001", name="Phone", category="digital", price=3999.0)
        ],
        recall_strategy="mock",
    )
    r.agent_results["marketing_copy"] = MarketingCopyResult(
        copies=[{"product_id": "P001", "copy": "hi"}],
        prompt_template_used="active",
    )
    r.agent_results["inventory"] = InventoryResult(
        available_products=["P001"],
        low_stock_alerts=[{"product_id": "P001"}],
        purchase_limits={"P001": 2},
    )

    dumped = r.model_dump(mode="json")["agent_results"]

    assert "profile" in dumped["user_profile"]
    assert "products" in dumped["product_rec"]
    assert "recall_strategy" in dumped["product_rec"]
    assert "copies" in dumped["marketing_copy"]
    assert "prompt_template_used" in dumped["marketing_copy"]
    assert "available_products" in dumped["inventory"]
    assert "low_stock_alerts" in dumped["inventory"]
    assert "purchase_limits" in dumped["inventory"]

    # 值也要真的带出来，不只是一个空壳字段
    assert dumped["product_rec"]["products"][0]["product_id"] == "P001"
    assert dumped["inventory"]["available_products"] == ["P001"]
    assert dumped["inventory"]["purchase_limits"] == {"P001": 2}


def test_base_agent_result_still_serializes() -> None:
    """
    降级路径必须仍然能序列化。

    `BaseAgent._fallback()` 在超时 / 熔断 / 异常时返回的是【基类】
    `AgentResult`。若把声明改成联合类型（UserProfileResult | ...），
    这条路径会校验失败 —— 故障注入的 4/4 HTTP 200 会变成 500。

    这条测试就是那个约束的守门人。
    """
    r = _response()
    r.agent_results["product_rec"] = AgentResult(
        agent_name="product_rec",
        success=False,
        error="product_rec exceeded 5.0s budget",
        confidence=0.0,
    )

    dumped = r.model_dump(mode="json")["agent_results"]["product_rec"]

    assert dumped["agent_name"] == "product_rec"
    assert dumped["success"] is False
    assert "exceeded" in dumped["error"]
    # 基类没有 products —— 降级结果本就不该伪装成有业务字段的样子
    assert "products" not in dumped


def test_serialization_does_not_require_union_membership() -> None:
    """
    新增一个【未登记】的 AgentResult 子类时，序列化不应报错。

    这条守的是"会复发"：用联合类型的话，每加一个 Agent 都得记得
    回来改 schemas —— 忘了就静默截断。SerializeAsAny 不需要登记。
    """

    class BrandNewResult(AgentResult):
        # agent_name 在基类里是必填（无默认值），所以这里必须显式传
        brand_new_field: str = "hello"

    r = _response()
    r.agent_results["brand_new"] = BrandNewResult(agent_name="brand_new")

    dumped = r.model_dump(mode="json")["agent_results"]["brand_new"]
    assert dumped["brand_new_field"] == "hello"
