"""
接口级测试 —— 5 条路由的状态码、响应契约与错误结构。

    为什么需要这个文件
    ────────────────
    加它之前，5 条路由【一条测试都没有】。168 个单测覆盖的是
    harness / agent / service 的内部行为 —— 它们一个 HTTP 请求都不发。
    后果是：路由签名改了、状态码变了、响应模型漏了字段，
    测试全绿，而线上接口已经坏了。测试数量和接口正确性之间没有必然关系。

    这一层的定位是"装配是否正确"，不是"业务逻辑是否正确"：
      - （是的）路由存在吗、路径对吗、状态码对吗
      - （是的）响应是不是符合声明的 response_model
      - （是的）错误响应是不是我们统一的 {code, message, request_id} 结构
      - （是的）中间件有没有按预期生效（X-Request-ID / 鉴权 / CORS）
      - （不是）LLM 返回的东西对不对 —— 那是 eval/ 的活

    所以这里把 4 个 Agent 换成桩，但【保留真实编排器、真实路由、
    真实中间件】。只桩在 LLM 边界上，是最划算的切法：
    桩得太早（把 supervisor 也换掉）就测不到序列化那层了，
    而 agent_results 的字段截断 bug 恰好就发生在那一层。

    全部离线：不发网络请求，不调 LLM，不依赖 Redis / MCP。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
from agents.product_rec_agent import MOCK_PRODUCTS
from config.settings import Settings
from models.schemas import (
    InventoryResult,
    MarketingCopyResult,
    ProductRecResult,
    UserProfile,
    UserProfileResult,
    UserSegment,
)
from web import install_http_layer
from web.errors import (
    CODE_INTERNAL,
    CODE_INVALID_REQUEST,
    CODE_METHOD_NOT_ALLOWED,
    CODE_NOT_FOUND,
    CODE_RATE_LIMITED,
    CODE_SERVICE_UNAVAILABLE,
    CODE_UNAUTHORIZED,
)
from web.middleware import ApiKeyMiddleware, RequestIdMiddleware
from web.ratelimit import RateLimitMiddleware

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── 桩 Agent ─────────────────────────────────────────────────────
#
# 刻意让桩产出【子类专属字段】（profile / products / copies /
# available_products / low_stock_alerts / purchase_limits）——
# 它们是 SerializeAsAny 那条修复的观测点。桩不产出这些字段的话，
# test_response_schema 想守的那个回归在这条路径上就守不住了。


class _StubProfileAgent:
    async def run(self, **kwargs: Any) -> UserProfileResult:
        return UserProfileResult(
            agent_name="user_profile",
            success=True,
            latency_ms=1.0,
            confidence=0.9,
            profile=UserProfile(
                user_id=kwargs.get("user_id", "u_stub"),
                segments=[UserSegment.ACTIVE],
                preferred_categories=["耳机"],
                real_time_tags={"source": "stub"},
            ),
        )


class _StubProductRecAgent:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run(self, **kwargs: Any) -> ProductRecResult:
        self.calls.append(kwargs)
        num_items = kwargs.get("num_items", 10)
        candidates = kwargs.get("candidates") or list(MOCK_PRODUCTS)
        return ProductRecResult(
            agent_name="product_rec",
            success=True,
            latency_ms=2.0,
            products=list(candidates)[:num_items],
            recall_strategy="stub",
        )


class _StubInventoryAgent:
    async def run(self, **kwargs: Any) -> InventoryResult:
        ids = [p.product_id for p in kwargs.get("products", [])]
        return InventoryResult(
            agent_name="inventory",
            success=True,
            latency_ms=0.1,
            available_products=ids,
            low_stock_alerts=[],
            purchase_limits={},
            data={"source": "fallback"},
        )


class _StubCopyAgent:
    async def run(self, **kwargs: Any) -> MarketingCopyResult:
        products = kwargs.get("products", [])
        return MarketingCopyResult(
            agent_name="marketing_copy",
            success=True,
            latency_ms=3.0,
            copies=[
                {"product_id": p.product_id, "copy": f"{p.name} 的文案"}
                for p in products
            ],
            prompt_template_used="stub",
        )


# ── 装置 ─────────────────────────────────────────────────────────


@pytest.fixture
def client() -> TestClient:
    """
    不带 with 的 TestClient：**不进入 lifespan**。

    区别很重要：进入 lifespan 会执行 build_recommendation_graph()，
    这没问题；但它同时会改 main.rec_graph 这个模块级全局，
    让"graph 路由"的用例依赖执行顺序。
    启动路径本身由 test_lifespan_starts_without_external_dependencies 单独覆盖。
    """
    return TestClient(main.app)


@pytest.fixture
def stubbed_supervisor(monkeypatch: pytest.MonkeyPatch) -> Any:
    """
    真编排器 + 桩 Agent。

    只桩在 LLM 边界上，所以 request_context / usage_scope /
    HarnessReport 组装 / pydantic 序列化这几层仍然是【真的】。
    """
    from orchestrator.supervisor import SupervisorOrchestrator

    orch = SupervisorOrchestrator()
    monkeypatch.setattr(orch, "user_profile_agent", _StubProfileAgent())
    monkeypatch.setattr(orch, "product_rec_agent", _StubProductRecAgent())
    monkeypatch.setattr(orch, "inventory_agent", _StubInventoryAgent())
    monkeypatch.setattr(orch, "marketing_copy_agent", _StubCopyAgent())
    # main.py 的路由是调用 get_supervisor()（惰性，@lru_cache）而不是读一个
    # 模块级变量 —— 所以这里替换的是那个取用函数，不是 main.supervisor 这个名字。
    monkeypatch.setattr(main, "get_supervisor", lambda: orch)
    return orch


def _minimal_app(**overrides: Any) -> TestClient:
    """
    一个最小应用，只用于测接入层本身（错误结构 / 鉴权 / CORS）。

    为什么不直接在 main.app 上加临时路由：那会污染模块级单例，
    而这个应用的 lifespan 一进来就建真的图。这里只要两个端点就够。
    """
    app = FastAPI()

    @app.get("/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "healthy"}

    @app.get("/boom")
    async def boom() -> dict[str, str]:
        # 故意在异常里塞一个"看起来像密钥/内网地址"的串。
        # 断言它【不】出现在响应体里 —— 这是脱敏那条设计的验收点。
        raise RuntimeError("secret-dsn://admin:hunter2@internal-host:5432/prod")

    install_http_layer(app, Settings(**overrides))
    return TestClient(app, raise_server_exceptions=False)


# ── GET /health ─────────────────────────────────────────────────


def test_health_returns_expected_contract(client: TestClient) -> None:
    resp = client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "healthy"
    # 这几个字段是前端要用的：它靠它们决定"依赖状态"面板显示什么、
    # 以及要不要带 X-API-Key。少了任何一个，前端就只能去猜。
    for key in (
        "model",
        "feature_store_enabled",
        "mcp_wms_enabled",
        "api_key_enabled",
    ):
        assert key in body, f"/health 少了 {key} —— 前端要靠它判断依赖状态"


def test_optional_dependencies_are_off_by_default(client: TestClient) -> None:
    """
    守项目约定：新接入的外部依赖【默认关闭】。

    这条断言看着琐碎，但它守的是"零破坏"这件事 ——
    一旦谁的 .env 或默认值被改成 true，测试会立刻红，
    而不是等到某台没装 Redis 的机器上才发现。
    """
    body = client.get("/health").json()

    assert body["feature_store_enabled"] is False
    assert body["mcp_wms_enabled"] is False
    assert body["api_key_enabled"] is False


def test_lifespan_starts_without_external_dependencies() -> None:
    """
    启动路径要能在"什么都没装"的情况下走完：
    build_recommendation_graph() 要成功，且特征层关闭时不去连 Redis。
    """
    with TestClient(main.app) as scoped:
        assert scoped.get("/health").status_code == 200


# ── X-Request-ID 与请求上下文 ────────────────────────────────────


def test_every_response_carries_request_id_header(client: TestClient) -> None:
    # 成功响应与错误响应都要带 —— 用户报错时提交的往往是后者。
    for resp in (client.get("/health"), client.get("/api/v1/does-not-exist")):
        rid = resp.headers.get("X-Request-ID")
        assert rid, f"{resp.status_code} 响应没有 X-Request-ID"
        assert len(rid) >= 8


def test_client_supplied_request_id_is_reused(client: TestClient) -> None:
    """
    上游（网关、前端代理）已经带了 id 就沿用。

    否则同一次调用会产生两个 id，排查时各查一半日志。
    """
    resp = client.get("/health", headers={"X-Request-ID": "rid-from-upstream"})

    assert resp.headers["X-Request-ID"] == "rid-from-upstream"


def test_malformed_incoming_request_id_is_replaced(client: TestClient) -> None:
    """
    超长的 incoming id 必须被丢掉，而不是原样回显。

    这个值会进日志字段和响应头 —— 不校验等于把这个格式的控制权
    交给调用方，一个 4KB 的头就能把日志淹掉。
    """
    resp = client.get("/health", headers={"X-Request-ID": "x" * 500})

    assert resp.headers["X-Request-ID"] != "x" * 500
    assert len(resp.headers["X-Request-ID"]) <= 64


# ── GET /api/v1/metrics ─────────────────────────────────────────


def test_metrics_returns_all_sections(client: TestClient) -> None:
    resp = client.get("/api/v1/metrics")

    assert resp.status_code == 200
    body = resp.json()
    for key in ("agents", "business", "breakers", "llm", "pricing", "tools"):
        assert key in body, f"/metrics 少了 {key} 段"


def test_metrics_lists_builtin_tools_when_mcp_is_off(client: TestClient) -> None:
    """
    MCP 关闭时应当只有内置工具。

    这个断言的实用价值：它是"当前接了哪些工具"的可观测口径，
    也是判断 MCP 到底装上没有最快的办法（开着会多几个）。
    """
    tools = client.get("/api/v1/metrics").json()["tools"]
    names = {t["name"] if isinstance(t, dict) else t for t in tools}

    assert {"get_metrics", "list_products"} <= names


# ── POST /api/v1/recommend ──────────────────────────────────────


def test_recommend_returns_full_contract(
    client: TestClient, stubbed_supervisor: Any
) -> None:
    resp = client.post(
        "/api/v1/recommend",
        json={"user_id": "u_api_test", "scene": "homepage", "num_items": 3},
    )

    assert resp.status_code == 200
    body = resp.json()

    assert body["user_id"] == "u_api_test"
    assert len(body["products"]) == 3
    assert len(body["marketing_copies"]) == 3
    assert set(body["agent_results"]) == {
        "user_profile",
        "product_rec",
        "marketing_copy",
        "inventory",
    }
    assert body["harness"] is not None
    assert set(body["harness"]) == {"usage", "agents", "breakers"}


def test_recommend_does_not_truncate_agent_subclass_fields(
    client: TestClient, stubbed_supervisor: Any
) -> None:
    """
    守住 SerializeAsAny 那条修复在【HTTP 边界】上仍然成立。

    agent_results 声明为 dict[str, AgentResult]，pydantic 默认按声明类型
    序列化，子类专属字段会被【静默】丢掉（不报错、不打日志）。
    tests/test_response_schema.py 已经在模型层守了一道，但模型层过得了、
    路由层因为 response_model 二次校验又被削掉，也是可能的 ——
    所以这里在真正过一遍 HTTP 之后再验一次。
    """
    body = client.post(
        "/api/v1/recommend",
        json={"user_id": "u_api_test", "num_items": 2},
    ).json()

    results = body["agent_results"]
    assert "profile" in results["user_profile"], "画像的子类字段被截断了"
    assert "products" in results["product_rec"], "召回的 products 被截断了"
    assert "copies" in results["marketing_copy"], "文案的 copies 被截断了"
    assert "available_products" in results["inventory"], "库存的字段被截断了"


def test_recommend_request_id_matches_header(
    client: TestClient, stubbed_supervisor: Any
) -> None:
    """
    响应体里的 request_id 必须和 X-Request-ID 响应头是同一个值。

    不一致的话，用户拿着头里的 id 去 grep 日志会 grep 不到 ——
    表现像"日志丢了"，实际是两套 id。
    """
    resp = client.post("/api/v1/recommend", json={"user_id": "u_api_test"})

    assert resp.json()["request_id"] == resp.headers["X-Request-ID"]


def test_recommend_survives_degraded_agents(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, stubbed_supervisor: Any
) -> None:
    """
    桩出来的编器里，库存 Agent 报降级（source=fallback），
    接口仍必须 200 —— 这是整个项目"挂掉也不怕"的最小验收点。
    """
    resp = client.post("/api/v1/recommend", json={"user_id": "u_api_test"})

    assert resp.status_code == 200
    assert resp.json()["products"], "降级路径下仍应返回商品"


# ── 请求校验与错误结构 ────────────────────────────────────────────


def test_missing_required_field_returns_envelope(client: TestClient) -> None:
    resp = client.post("/api/v1/recommend", json={"scene": "homepage"})

    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == CODE_INVALID_REQUEST
    assert err["request_id"], "422 也要带 request_id，否则用户报错时无法定位"
    assert "details" in err


def test_wrong_type_returns_envelope(client: TestClient) -> None:
    resp = client.post("/api/v1/recommend", json={"user_id": "u", "num_items": "很多"})

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == CODE_INVALID_REQUEST


def test_unknown_route_returns_envelope(client: TestClient) -> None:
    """
    404 也要是统一结构。

    不加处理器时这里是 {"detail": "Not Found"} —— 前端得为它写一套
    特例分支。归一化之后前端只有一种解析路径。
    """
    resp = client.get("/api/v1/definitely-not-here")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == CODE_NOT_FOUND


def test_method_not_allowed_returns_envelope(client: TestClient) -> None:
    resp = client.get("/api/v1/recommend")

    assert resp.status_code == 405
    assert resp.json()["error"]["code"] == CODE_METHOD_NOT_ALLOWED


# ── 全局异常处理器 ───────────────────────────────────────────────


def test_unhandled_exception_is_normalized() -> None:
    resp = _minimal_app().get("/boom")

    assert resp.status_code == 500
    err = resp.json()["error"]
    assert err["code"] == CODE_INTERNAL
    assert err["request_id"], "500 的 request_id 为空 —— 排查时无法把响应和日志对上"
    # 关键不变式：响应体里的 id 必须和响应头里的是同一个。
    # 这条曾经真的红的：兜底处理器跑在 ServerErrorMiddleware 里（所有自定义
    # 中间件的外层），异常穿过 RequestIdMiddleware 时 contextvar 已被复位，
    # 只有 scope 上那份还在。这条断言就是那次修复留下的钉子。
    assert err["request_id"] == resp.headers["X-Request-ID"]


def test_unhandled_exception_does_not_leak_internals() -> None:
    """
    异常字符串里的内网地址 / 口令片段绝不能出现在响应体里。

    这是"对外一句通用文案、对内完整堆栈"那条设计的验收点。
    HTTP 客户端拿到的异常消息里经常混着 DSN、URL、甚至 token。
    """
    text = _minimal_app().get("/boom").text

    assert "secret-dsn" not in text
    assert "hunter2" not in text
    assert "internal-host" not in text
    # 但排查能力不能丢：请求 ID 必须在，用户贴它给你，你就能 grep 到堆栈。
    assert "请求 ID" in text


# ── POST /api/v1/recommend/graph ────────────────────────────────


def _stub_graph() -> Any:
    """
    假的 LangGraph 流水线。

    刻意让 agent_results 用【图内部的键名】（product_recall / rerank），
    因为归一化正是这一版要守的东西 —— 桩直接吐规范键名的话，
    归一化坏掉了测试也发现不了。
    """

    class _Graph:
        async def ainvoke(self, state: dict[str, Any]) -> dict[str, Any]:
            final = list(MOCK_PRODUCTS[:2])
            return {
                **state,
                "final_products": final,
                "marketing_copies": [{"product_id": p.product_id, "copy": "x"} for p in final],
                "agent_results": {
                    "user_profile": UserProfileResult(
                        agent_name="user_profile",
                        success=True,
                        profile=UserProfile(user_id="u_stub"),
                    ),
                    "product_recall": ProductRecResult(
                        agent_name="product_rec",
                        success=True,
                        products=list(MOCK_PRODUCTS[:4]),
                    ),
                    "rerank": ProductRecResult(
                        agent_name="product_rec", success=True, products=final
                    ),
                    "inventory": InventoryResult(
                        agent_name="inventory",
                        success=True,
                        available_products=[p.product_id for p in final],
                    ),
                    "marketing_copy": MarketingCopyResult(
                        agent_name="marketing_copy", success=True, copies=[]
                    ),
                },
                "total_latency_ms": 1.0,
            }

    return _Graph()


def test_graph_route_uses_the_configured_graph(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    只验路由装配：把 rec_graph 换成桩，确认路由能取到它、
    返回值形状对、并且 request_id 与响应头一致。

    不跑真的 LangGraph：那会一路走到真 Agent 和真 LLM，
    而这里是接口级测试，不是端到端评测（那个在 eval/）。
    """
    monkeypatch.setattr(main, "rec_graph", _stub_graph())

    resp = client.post("/api/v1/recommend/graph", json={"user_id": "u_graph"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == "u_graph"
    assert len(body["products"]) == 2
    assert body["request_id"] == resp.headers["X-Request-ID"]


def test_graph_route_returns_the_same_contract_as_main_endpoint(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, stubbed_supervisor: Any
) -> None:
    """
    两条推荐路径的【顶层结构必须完全一致】。

    这条是 P2 的核心：在此之前 graph 端点返回的是一个缩小版对象
    （没有 agent_results、没有 harness、连 response_model 都没有），
    于是前端要为同一个"推荐"概念写两套解析，而各 Agent 耗时 /
    熔断状态 / token 账本在 graph 路径上完全不可见。

    用"两边键集合相等"来断言，而不是逐个断言键存在 ——
    后者在有人【新增】一个键时不会红，前者会，那就逼着他同步两条路径。
    """
    monkeypatch.setattr(main, "rec_graph", _stub_graph())

    graph_body = client.post(
        "/api/v1/recommend/graph", json={"user_id": "u_graph"}
    ).json()
    main_body = client.post(
        "/api/v1/recommend", json={"user_id": "u_api_test"}
    ).json()

    assert set(graph_body) == set(main_body), (
        "两条推荐路径的响应顶层结构不一致 —— 前端就得写两套解析"
    )
    assert set(graph_body["harness"]) == set(main_body["harness"])


def test_graph_route_normalizes_agent_result_keys(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    图内部的 product_recall / rerank 必须归一化成契约里的 product_rec。

    不归一化的话，前端会看到 5 个键里夹着一个它不认识的 product_recall，
    而少的那个 product_rec 又恰好是它真正需要的。
    """
    monkeypatch.setattr(main, "rec_graph", _stub_graph())

    results = client.post(
        "/api/v1/recommend/graph", json={"user_id": "u_graph"}
    ).json()["agent_results"]

    assert set(results) == {
        "user_profile",
        "product_rec",
        "marketing_copy",
        "inventory",
    }, f"agent_results 键名没归一化: {sorted(results)}"
    # product_rec 必须是【重排后】的结果（Phase 1 首次召回是中间产物，不进响应）
    assert len(results["product_rec"]["products"]) == 2


def test_graph_route_without_graph_returns_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    流水线没装配好时返回 503 + 统一错误信封。

    改之前是 `200` + body 里的 `{"error": "Graph not initialized"}` ——
    客户端按 status code 判断成败时会认为它成功了，然后拿
    `{"error": ...}` 去当正常响应解析。这是"用 200 表达失败"的经典坑。

    503 而不是 500：语义是"暂时不可用"，重启解决不了它 ——
    与 /ready 的结论保持一致（两者看的是同一个条件）。
    """
    monkeypatch.setattr(main, "rec_graph", None)

    resp = client.post("/api/v1/recommend/graph", json={"user_id": "u_graph"})

    assert resp.status_code == 503
    err = resp.json()["error"]
    assert err["code"] == CODE_SERVICE_UNAVAILABLE
    assert err["request_id"] == resp.headers["X-Request-ID"]


def test_graph_route_is_declared_in_openapi(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    必须有 response_model —— 否则 OpenAPI 里没有 schema，
    前端生成不了类型（这正是 P2 里"契约不一致"的一部分）。
    """
    schema = client.get("/openapi.json").json()
    op = schema["paths"]["/api/v1/recommend/graph"]["post"]

    ref = op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    assert ref.endswith("RecommendationResponse")


# ── POST /api/v1/copilot ────────────────────────────────────────


class _FakeRegistry:
    """只需要 len() 和 snapshot() —— 路由用到的就这两处。"""

    def __init__(self, size: int) -> None:
        self._size = size

    def __len__(self) -> int:
        return self._size


class _FakeCopilot:
    def __init__(self) -> None:
        self.registry: Any = None
        self.asked: list[tuple[str, str]] = []

    def reset(self, session_id: str) -> None:
        pass

    async def ask(self, message: str, session_id: str = "default") -> Any:
        self.asked.append((message, session_id))

        class _Reply:
            reply = "库存充足，暂无需要处理的商品。"
            stop_reason = "completed"
            steps = [object()]
            tool_results = [
                {"step": 1, "tool": "list_products", "ok": True, "degraded": False}
            ]
            usage = {"llm_calls": 1, "input_tokens": 10, "output_tokens": 5}
            latency_ms = 12.5

        return _Reply()


def test_copilot_returns_reply_with_tool_trace(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import copilot.router as copilot_router

    fake = _FakeCopilot()

    async def _registry() -> _FakeRegistry:
        return _FakeRegistry(2)

    async def _agent() -> _FakeCopilot:
        return fake

    monkeypatch.setattr(copilot_router, "get_tool_registry", _registry)
    monkeypatch.setattr(copilot_router, "_get_copilot", _agent)

    resp = client.post(
        "/api/v1/copilot", json={"message": "现在有缺货商品吗？", "session_id": "s1"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == "s1"
    assert body["stop_reason"] == "completed"
    assert body["steps"] == 1
    # 工具调用轨迹必须出现在响应里 —— 前端要把"模型自己查了什么"画出来，
    # 那是这个接口最有展示价值的部分。
    assert body["tool_calls"][0]["tool"] == "list_products"
    assert fake.asked == [("现在有缺货商品吗？", "s1")]


def test_copilot_without_tools_returns_503_envelope(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import copilot.router as copilot_router

    async def _empty_registry() -> _FakeRegistry:
        return _FakeRegistry(0)

    monkeypatch.setattr(copilot_router, "get_tool_registry", _empty_registry)

    resp = client.post("/api/v1/copilot", json={"message": "在吗"})

    assert resp.status_code == 503
    # 503 与 ServiceUnavailableError 共用一个 code —— 前端只需一条分支。
    assert resp.json()["error"]["code"] == CODE_SERVICE_UNAVAILABLE


def test_copilot_rejects_empty_message(client: TestClient) -> None:
    resp = client.post("/api/v1/copilot", json={"message": ""})

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == CODE_INVALID_REQUEST


# ── 鉴权中间件 ──────────────────────────────────────────────────


def test_api_key_disabled_by_default_allows_anonymous_access(
    client: TestClient,
) -> None:
    """默认配置下不装鉴权中间件，匿名请求照常通过。"""
    assert client.get("/api/v1/metrics").status_code == 200


def test_missing_api_key_is_rejected() -> None:
    resp = _minimal_app(api_key_enabled=True, api_key="s3cret").get("/ping")

    assert resp.status_code == 401
    err = resp.json()["error"]
    assert err["code"] == CODE_UNAUTHORIZED
    assert err["request_id"], "401 也要带 request_id"
    assert resp.headers["WWW-Authenticate"].startswith("ApiKey")


def test_wrong_api_key_is_rejected() -> None:
    resp = _minimal_app(api_key_enabled=True, api_key="s3cret").get(
        "/ping", headers={"X-API-Key": "wrong"}
    )

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == CODE_UNAUTHORIZED


def test_correct_api_key_in_header_is_accepted() -> None:
    resp = _minimal_app(api_key_enabled=True, api_key="s3cret").get(
        "/ping", headers={"X-API-Key": "s3cret"}
    )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_correct_api_key_as_bearer_is_accepted() -> None:
    """Authorization: Bearer 也能过 —— 方便复用现成的客户端库。"""
    resp = _minimal_app(api_key_enabled=True, api_key="s3cret").get(
        "/ping", headers={"Authorization": "Bearer s3cret"}
    )

    assert resp.status_code == 200


def test_health_is_exempt_from_api_key() -> None:
    """
    /health 必须免鉴权。

    否则编排系统的探活会被 401 挡在门外，服务看起来像已经死了 ——
    一个"安全加固"把可用性打掉，是典型的加过头。
    """
    resp = _minimal_app(api_key_enabled=True, api_key="s3cret").get("/health")

    assert resp.status_code == 200


def test_enabled_but_empty_key_fails_fast() -> None:
    """
    启用了鉴权却没填 key —— 必须启动就炸，而不是静默放行所有请求。

    静默放行比不鉴权更糟：它让人以为已经受保护了。
    """
    with pytest.raises(RuntimeError, match="ECOM_API_KEY"):
        _minimal_app(api_key_enabled=True, api_key="")


def test_exempt_path_matching_normalizes_trailing_slash() -> None:
    mw = ApiKeyMiddleware(app=None, api_key="k", exempt_paths={"/health"})  # type: ignore[arg-type]

    assert mw._is_exempt("/health")
    assert mw._is_exempt("/health/"), "少写/多写尾斜杠都不该绕过（或误挡）免鉴权"
    assert not mw._is_exempt("/api/v1/metrics")
    assert not mw._is_exempt("/healthz"), "必须是精确匹配，不能变成前缀匹配"


# ── CORS ────────────────────────────────────────────────────────


def test_configured_origin_is_allowed() -> None:
    resp = _minimal_app().get("/ping", headers={"Origin": "http://localhost:5173"})

    assert resp.headers.get("Access-Control-Allow-Origin") == "http://localhost:5173"


def test_unlisted_origin_gets_no_cors_allow_header() -> None:
    resp = _minimal_app().get("/ping", headers={"Origin": "http://evil.example"})

    assert "Access-Control-Allow-Origin" not in resp.headers


def test_preflight_succeeds_without_api_key() -> None:
    """
    预检请求不带自定义头，因此【必须】不需要鉴权。

    这条能过是因为 CORS 中间件在最外层、自己就把 OPTIONS 处理掉了。
    顺序写反（CORS 加到鉴权内层）时这条会红 —— 而线上表现是
    "浏览器报一个含糊的 CORS error，完全看不到真实的 401"。
    """
    resp = _minimal_app(api_key_enabled=True, api_key="s3cret").options(
        "/ping",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "X-API-Key",
        },
    )

    assert resp.status_code == 200
    assert resp.headers["Access-Control-Allow-Origin"] == "http://localhost:5173"
    assert "X-API-Key" in resp.headers["Access-Control-Allow-Headers"]


def test_cors_exposes_request_id_header() -> None:
    """
    X-Request-ID 必须出现在 Access-Control-Expose-Headers 里。

    浏览器不允许 JS 读取未声明的响应头。不 expose 的话，这个头在
    devtools 的网络面板里看得到、代码里却读不到 —— 而"看着有却拿不到"
    是最耗时间的一类问题：前端会以为是自己写错了。

    注意只能在【简单响应】上断言：CORS 预检响应里 Starlette 不带
    Expose-Headers（只有 Allow-Methods / Allow-Headers / Max-Age）。
    """
    resp = _minimal_app().get("/ping", headers={"Origin": "http://localhost:5173"})

    assert "X-Request-ID" in resp.headers.get("Access-Control-Expose-Headers", "")


def test_default_cors_is_not_wildcard() -> None:
    """
    守 P1 那条修复：默认来源不能再是 "*"。

    通配符是"忘了改配置"的典型形态，默认值应当落在安全的那一侧。

    这里直接读【字段默认值】而不是 Settings()：后者会读 .env，
    某人本地把 ECOM_CORS_ALLOW_ORIGINS 设成 * 就会让这条测试红 ——
    但那是配置问题，不是默认值退化了，测试不该混为一谈。
    """
    default = Settings.model_fields["cors_allow_origins"].default

    assert default != "*"
    assert "*" not in default
    assert all(o.startswith("http") for o in Settings(cors_allow_origins=default).cors_origins)


def test_cors_wildcard_setting_is_still_honoured() -> None:
    """显式要通配符时仍然可用 —— 只是必须自己写出来。"""
    assert Settings(cors_allow_origins="*").cors_origins == ["*"]
    assert Settings(cors_allow_origins="* , http://x").cors_origins == ["*"]


def test_exempt_paths_parsing_ignores_blank_entries() -> None:
    settings = Settings(api_key_exempt_paths="/health, ,/docs,")

    assert settings.api_key_exempt_set == {"/health", "/docs"}


# ── 结构化日志 ──────────────────────────────────────────────────


def test_logs_are_real_json_in_a_subprocess() -> None:
    """
    验证"结构化 JSON 日志"这句宣称。

    为什么用【子进程】而不是在测试进程里 configure 一下：
    structlog 的配置是进程级全局的，而这个测试里的两条关键设置
    （processor 链 + 过滤级别）都会影响同进程内其它测试的断言。
    本项目已经在"全局状态跨测试累积"上踩过一次（见 conftest 的两个
    autouse 装置），不值得为一条用例再冒一次险。
    子进程还顺带证明了"从零启动时它确实输出 JSON"。
    """
    snippet = (
        "import sys; sys.path.insert(0, "
        f"{BACKEND_DIR!r});"
        "from logging_setup import configure_logging;"
        "import structlog;"
        "configure_logging(True, 'INFO');"
        "structlog.get_logger().info('smoke.event', k='中文值');"
    )
    proc = subprocess.run(
        [sys.executable, "-c", snippet],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )

    assert proc.returncode == 0, f"子进程失败: {proc.stderr}"

    parsed = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    # PrintLogger 只输出我们这一条事件 —— 有几行就应当有几条事件。
    assert parsed, f"没有输出任何日志行: {proc.stdout!r}"

    event = next(p for p in parsed if p.get("event") == "smoke.event")
    assert event["k"] == "中文值", (
        "中文被转义成 \\uXXXX 了 —— JSONRenderer 的 ensure_ascii=False 没生效，"
        "那样 grep 中文日志会一条都搜不到"
    )
    assert event["level"] == "info"
    assert "timestamp" in event, "缺少时间戳，跨服务无法按时间排序"


# ── GET /ready ──────────────────────────────────────────────────


def test_ready_reports_ready_when_pipeline_is_built(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "rec_graph", _stub_graph())

    resp = client.get("/ready")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["checks"]["pipeline"] == "ok"


def test_ready_returns_503_when_pipeline_is_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "rec_graph", None)

    resp = client.get("/ready")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["pipeline"] == "missing"


def test_ready_does_not_gate_on_optional_dependencies(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    可选依赖不可用【不】影响就绪。

    这是本项目的核心设计使然：Redis 挂了走 fallback、MCP 挂了回落
    Product.stock、LLM 挂了返回降级结果 —— 全都有实测 200 的证据。
    把它们算进就绪判断，会让编排系统摘掉一个其实还在正常降级服务的实例，
    等于亲手丢掉降级能力。

    所以它们只被【汇报】在 optional 里。
    """
    monkeypatch.setattr(main, "rec_graph", _stub_graph())

    body = client.get("/ready").json()

    assert set(body["optional"]) == {"feature_store", "mcp_wms"}
    # conftest 保证了这两个开关默认关闭
    assert body["optional"]["feature_store"] == "disabled"
    assert body["optional"]["mcp_wms"] == "disabled"
    assert body["status"] == "ready", "可选依赖关闭不该让实例不就绪"


def test_ready_is_a_probe_not_an_error_envelope(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    探针不走错误信封：body 是状态自述，结论由状态码承载。

    理由与 /health 一致 —— 它是给编排系统看的，不是给"遇到错误的用户"看的。
    """
    monkeypatch.setattr(main, "rec_graph", None)

    body = client.get("/ready").json()

    assert "error" not in body
    assert "status" in body


def test_health_and_ready_have_distinct_semantics(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    把"这两个接口不一样"固定下来。

    没有这条断言的话，很容易被后人"顺手合并成一个 /health" —— 那样
    "进程还活着"和"能不能提供服务"两种语义就又混在一起了，
    而它们的正确处置方式完全不同（重启 vs 等待）。
    """
    monkeypatch.setattr(main, "rec_graph", None)

    assert client.get("/health").status_code == 200, "进程活着就该 200"
    assert client.get("/ready").status_code == 503, "装配未完成就该 503"


# ── 限流 ────────────────────────────────────────────────────────


def test_rate_limit_is_off_by_default(client: TestClient) -> None:
    """默认配置下没有装限流中间件，连打不会 429。"""
    assert {client.get("/health").status_code for _ in range(10)} == {200}


def test_default_rate_limit_setting_is_disabled() -> None:
    assert Settings.model_fields["rate_limit_enabled"].default is False


def test_rate_limit_blocks_after_the_limit() -> None:
    app = _minimal_app(
        rate_limit_enabled=True, rate_limit_requests=2, rate_limit_window_s=60
    )

    assert app.get("/ping").status_code == 200
    assert app.get("/ping").status_code == 200

    third = app.get("/ping")
    assert third.status_code == 429
    err = third.json()["error"]
    assert err["code"] == CODE_RATE_LIMITED
    assert err["request_id"] == third.headers["X-Request-ID"], (
        "429 必须也带 request_id —— 它和 401/500 是同一类问题："
        "不经过 RequestIdMiddleware 包装的响应会丢头"
    )
    assert err["details"]["retry_after_s"] > 0
    assert int(third.headers["Retry-After"]) >= 1
    assert third.headers["X-RateLimit-Remaining"] == "0"


def test_rate_limit_does_not_gate_probes() -> None:
    """
    探针必须免限流。

    被限流挡住的探活会让编排系统认为实例挂了并把它摘掉 ——
    而它其实好好的。这就是为什么 /health 与 /ready 都在免检名单里。
    """
    app = _minimal_app(
        rate_limit_enabled=True, rate_limit_requests=1, rate_limit_window_s=60
    )

    assert app.get("/ping").status_code == 200
    assert app.get("/ping").status_code == 429, "业务路径该被限流"

    assert {app.get("/health").status_code for _ in range(5)} == {200}


def test_rate_limit_buckets_by_credential() -> None:
    """
    不同凭据各算各的桶。

    如果按 IP 分桶，同一台机器上的两个调用方会互相影响 ——
    而且一个调用方能把另一个"限"死。
    """
    app = _minimal_app(
        rate_limit_enabled=True, rate_limit_requests=1, rate_limit_window_s=60
    )

    assert app.get("/ping", headers={"X-API-Key": "key-a"}).status_code == 200
    assert app.get("/ping", headers={"X-API-Key": "key-a"}).status_code == 429
    assert app.get("/ping", headers={"X-API-Key": "key-b"}).status_code == 200


def test_rate_limit_window_slides() -> None:
    """
    窗口会滑动，所以等待时间是【确定的】而不是"再等一个整窗口"。

    这也是选滑动窗口而不是固定窗口的原因之一（另一个原因是固定窗口
    在边界处会放过两倍速率）。
    """
    app = _minimal_app(
        rate_limit_enabled=True, rate_limit_requests=1, rate_limit_window_s=0.3
    )

    assert app.get("/ping").status_code == 200
    assert app.get("/ping").status_code == 429

    time.sleep(0.35)

    assert app.get("/ping").status_code == 200, "旧记录滑出窗口后就该放行"


def test_rate_limit_response_is_readable_by_the_browser() -> None:
    """
    429 的 CORS 头与 expose 头都要在。

    CORS 头缺失 → 浏览器只报含糊的 "CORS error"，真实原因（429）被盖掉。
    expose 头缺失 → Retry-After 在 devtools 里看得到、代码里读不到，
    而它恰恰是前端唯一能拿到"还要等几秒"的地方。
    """
    app = _minimal_app(
        rate_limit_enabled=True, rate_limit_requests=1, rate_limit_window_s=60
    )
    origin = {"Origin": "http://localhost:5173"}

    app.get("/ping", headers=origin)
    resp = app.get("/ping", headers=origin)

    assert resp.status_code == 429
    assert resp.headers["Access-Control-Allow-Origin"] == "http://localhost:5173"
    exposed = resp.headers["Access-Control-Expose-Headers"]
    assert "Retry-After" in exposed


def test_rate_limit_rejects_a_useless_threshold() -> None:
    """limit=0 会让所有请求 429 —— 那是配置错误，不是"严格限流"，直接报出来。"""
    with pytest.raises(ValueError, match="阈值"):
        RateLimitMiddleware(  # type: ignore[arg-type]
            app=None, limit=0, window_s=60, exempt_paths=set()
        )

