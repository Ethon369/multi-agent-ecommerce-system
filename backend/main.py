"""
Multi-Agent E-Commerce Recommendation System — FastAPI Entry Point

Endpoints:
  POST /api/v1/recommend          - 获取个性化推荐（asyncio.gather 编排）
  POST /api/v1/recommend/graph    - 通过 LangGraph pipeline 推荐（同一响应契约）
  POST /api/v1/copilot            - 运营 Copilot（MCP Host，模型自主调工具）
  GET  /api/v1/metrics            - 查看系统监控指标
  GET  /health                    - 存活探针（liveness）
  GET  /ready                     - 就绪探针（readiness）
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from contextlib import asynccontextmanager
from typing import Any

import structlog
import uvicorn
from fastapi import FastAPI, Response

from config import get_settings
from harness import get_runtime
from harness.deps import (
    get_feature_store,
    get_metrics_collector,
    get_pricing,
    get_supervisor,
    get_tool_registry,
)
from harness.pricing import PRICING_AS_OF, PRICING_SOURCE
from logging_setup import configure_logging
from models.schemas import RecommendationRequest, RecommendationResponse
from orchestrator.graph import build_recommendation_graph, run_recommendation_graph
from web import install_http_layer
from web.errors import ServiceUnavailableError

settings = get_settings()

# 日志配置的位置是刻意的：必须在【任何日志真正落盘之前】调一次。
# 本项目所有模块都在 import 期就拿好了 logger，好在 get_logger() 返回的是
# 惰性代理、import 本身不产出日志 —— 所以"import 一圈 → 在这里配置 →
# 才开始服务"是安全的，且此后所有日志（含各 Agent 里那些一行都没改过的）
# 都会走 JSON。
#
# 在加这一行之前，configure_logging() 根本没有调用点（它在"删死代码"那轮
# 被删了），于是文档里的"结构化 JSON 日志"实际是 structlog 默认的彩色文本。
configure_logging(settings.log_json, settings.log_level)

logger = structlog.get_logger()


# 走组合根取共享单例 —— 与 graph.py、Copilot、MCP Server 用的是同一份。
# 此前这里是几套互不相知的实例，导致熔断状态在不同路径上不同步。
#
# ⚠️ 这里【刻意不】在模块级调 get_supervisor()。
#
# 原先那一行是 `supervisor = get_supervisor()`，而它会连锁构造 4 个 Agent
# 及其 LLM 客户端 —— 也就是把"读 .env、建 HTTP 客户端"变成 import main
# 的副作用。后果实测如下（全新 clone、没有 .env 时）：
#
#     $ pytest tests/
#     ERROR tests/test_api.py - openai.OpenAIError: Missing credentials
#
# 测试根本收集不起来。而 harness/deps.py 的 docstring 里写得很清楚，
# 惰性（@lru_cache）正是为了让"MCP Server 进程、测试进程未必需要全部依赖"
# 才选的 —— 模块级调用把那层设计意图抵消掉了。
#
# 所以改成在路由里取：get_supervisor() 已经被 @lru_cache 缓存，
# 首次调用之后就是一次字典查找，没有性能代价。
metrics_collector = get_metrics_collector()
rec_graph = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global rec_graph
    rec_graph = build_recommendation_graph()

    # 预热 Redis 连接：把【首次连接】的代价在启动时付掉，别让第一个
    # 用户请求付。实测首次连接约 2 秒（`localhost` 先试 IPv6 再回落 IPv4），
    # 而请求路径的超时只有 0.5 秒 —— 正好会把连接掐断，变成永久降级、
    # 每次请求都重新连、每次都超时。失败只记日志，不阻断启动。
    store = get_feature_store()
    if store is not None:
        await store.warmup()

    # 没配 LLM Key 时大声提示一次。放在这里（而不是 import 期报错）是有意的：
    # 缺凭据不该让进程起不来 —— 它等于"所有 Agent 都会降级"，那是这个项目
    # 本来就设计好的路径。但误配必须能在日志里一眼看到，否则表现为
    # "接口都能返回 200，只是结果全是降级的"，很难联想到是凭据问题。
    if not settings.llm_api_key:
        logger.error(
            "app.llm_key_missing",
            detail=(
                "ECOM_LLM_API_KEY 为空。服务可以启动，但所有 Agent 调用都会失败"
                "并返回降级结果。请在 backend/.env 里填入真实 Key。"
            ),
        )

    logger.info("app.startup", model=settings.llm_model)
    yield
    logger.info("app.shutdown")


app = FastAPI(
    title="Multi-Agent E-Commerce Recommendation System",
    description="用户画像Agent + 商品推荐Agent + 营销文案Agent + 库存决策Agent，并行+聚合模式",
    version="1.0.0",
    lifespan=lifespan,
)

# 接入层：异常处理器 + API Key 鉴权 + 请求 ID + CORS。
#
# 一次性装配而不是在这里散着 add_middleware —— 因为顺序有硬要求
# （后 add 的在外层：路由 → 鉴权 → 请求 ID → CORS），写反了不报错，
# 只会让 401 响应丢掉 CORS 头、或者鉴权失败没有 request_id。
# 顺序的理由写在 web/__init__.py 的模块 docstring 里。
#
# 换掉原来的 allow_origins=["*"]：来源现在从 ECOM_CORS_ALLOW_ORIGINS 读，
# 默认值是开发期端口。通配符是"忘了改配置"的典型形态，默认值应当给安全的那一侧。
install_http_layer(app, settings)

# 运营 Copilot（MCP Host 角色）：由模型自主决定调用哪些工具。
# 与主推荐接口共享熔断/指标/账本/工具注册表，但不共享会话状态。
from copilot.router import router as copilot_router  # noqa: E402

app.include_router(copilot_router)


@app.get("/health")
async def health():
    # 除了探活，这里还汇报【可选依赖的开关状态】。
    # 评测 runner 是另一个进程，用它自己的设置去判断"服务端开没开"会判错
    # （实测：给服务端加了 ECOM_FEATURE_STORE_ENABLED=true 而 runner 没加，
    # 用例被误判成配置不满足）。所以让服务端自己说。
    return {
        "status": "healthy",
        "model": settings.llm_model,
        "feature_store_enabled": settings.feature_store_enabled,
        "mcp_wms_enabled": settings.mcp_wms_enabled,
        # 前端需要知道要不要带 X-API-Key。同样是"让服务端自己说"的理由：
        # 前端读自己的构建期环境变量去猜服务端开没开，是会猜错的
        # （这正是 feature_store_enabled 当初被加进来的原因）。
        "api_key_enabled": settings.api_key_enabled,
    }


@app.get("/ready")
async def ready(response: Response) -> dict[str, Any]:
    """
    就绪探针。

        和 /health 的区别（这两者经常被混为一谈）
        ──────────────────────────────────────
        `/health` —— **存活**：进程还在、能响应 HTTP。它【不检查】任何依赖。
                     失败的含义是"这个进程该被重启"。
        `/ready`  —— **就绪**：这个实例此刻能不能履行它的契约。
                     失败的含义是"暂时别把流量给它"，但【不该】重启它 ——
                     "流水线还没装配完"重启一万次也一样。

        为什么可选依赖【不】决定就绪
        ──────────────────────────
        这个系统的核心设计是"外部依赖挂了也能降级交付"：Redis 挂了走
        fallback、MCP 挂了回落 `Product.stock`、LLM 挂了返回降级结果 ——
        全部实测返回 200。所以 Redis / MCP 不可用时这个实例【仍然是就绪的】，
        把它摘掉反而丢掉了降级能力（也丢掉了整个项目最想证明的那件事）。

        它们的状态照样如实汇报在 `optional` 里，给人看、给前端画依赖面板用，
        但不参与判断。

        真正决定就绪的只有一项：**流水线装配完了没有** ——
        没装配完的实例接到请求只能回 503。

    这里【不】走错误信封：它是给编排系统看的探针，body 是状态自述而不是
    错误描述，结论由状态码承载 —— 与 `/health` 保持一致。
    """
    checks = {"pipeline": "ok" if rec_graph is not None else "missing"}
    not_ready = [name for name, value in checks.items() if value != "ok"]

    if not_ready:
        logger.warning("app.not_ready", failed=not_ready)
        # 503 而不是 500：语义是"暂时不可用"，不是"内部错误"。
        # 500 会让编排系统以为该重启，而这里重启没有意义。
        response.status_code = 503

    return {
        "status": "not_ready" if not_ready else "ready",
        "checks": checks,
        "optional": await _probe_optional_dependencies(),
    }


async def _probe_optional_dependencies() -> dict[str, str]:
    """
    探测可选依赖的实时状态。只汇报，不参与就绪判断（理由见 /ready）。

    Redis 那条是【真实探测】（`FeatureStore.ping()`，0.5s 预算，永不抛）。
    MCP 那条只报开关 —— 因为 MCP 是 stdio 子进程，探活要付一次约 1.15 秒的
    子进程启动代价（实测），一个每几秒被打一次的探针不该干这件事。
    """
    optional: dict[str, str] = {
        "mcp_wms": "enabled" if settings.mcp_wms_enabled else "disabled",
    }
    store = get_feature_store()
    if store is None:
        # 开关关闭时连 redis 客户端都不会被构造（见 harness/deps.py），
        # 所以 "disabled" 是准确描述，而不是"未知"。
        optional["feature_store"] = "disabled"
    else:
        optional["feature_store"] = "ok" if await store.ping() else "unreachable"
    return optional


@app.post("/api/v1/recommend", response_model=RecommendationResponse)
async def recommend(request: RecommendationRequest) -> RecommendationResponse:
    """使用 Supervisor 编排器进行推荐（生产推荐用法）。"""
    # 惰性取单例：首次请求时才构造 Agent（见模块顶部关于 import 副作用的注释）。
    response = await get_supervisor().recommend(request)
    _collect_metrics(response)
    return response


@app.post("/api/v1/recommend/graph", response_model=RecommendationResponse)
async def recommend_via_graph(request: RecommendationRequest) -> RecommendationResponse:
    """
    使用 LangGraph 状态图进行推荐。

    与 `/api/v1/recommend` 是**同一个响应契约** —— 包括 `agent_results`
    与 `harness`。在此之前这里返回的是一个缩小版对象（连 response_model
    都没有），于是 OpenAPI 里没有 schema、前端要为同一个"推荐"概念写两套
    解析、而且**各 Agent 耗时 / 熔断状态 / token 账本在这条路径上完全不可见**
    —— 那正是这个项目最值钱的部分。

    统一之后还有个附带好处：两套编排器（asyncio.gather vs LangGraph）
    的输出可以直接对比，这才让"同时保留两套实现"有了意义。
    """
    if rec_graph is None:
        # 503 而不是"200 + body 里的 error 字段"。
        #
        # 这不是"请求成功但结果是个错误"，而是"服务现在提供不了这个能力"。
        # 客户端按 status code 判断成败 —— 200 会让它以为一切正常，
        # 于是把 {"error": ...} 当成正常响应去解析。
        # 状态码与 /ready 的结论一致：两者看的是同一个条件。
        raise ServiceUnavailableError("LangGraph 流水线尚未装配完成，请稍后重试。")

    response = await run_recommendation_graph(rec_graph, request)
    # 两条推荐路径的指标口径保持一致 —— 否则 /api/v1/metrics 会随
    # "调用者挑了哪个端点"而变化，那它就不是系统指标了。
    _collect_metrics(response)
    return response


@app.get("/api/v1/metrics")
async def get_metrics():
    """查看系统监控指标"""
    registry = await get_tool_registry()
    return {
        "agents": metrics_collector.get_agent_stats(),
        "business": metrics_collector.get_business_stats(),
        # 熔断状态按 agent 名索引，进程级共享 —— 两个编排器看到的是同一份健康状态
        "breakers": get_runtime().snapshot(),
        # LLM 用量累计（进程启动以来）。cost_usd 为 null 表示有模型查不到单价，
        # 不是"免费"。
        "llm": metrics_collector.get_llm_stats(),
        "pricing": {
            "as_of": PRICING_AS_OF,
            "source": PRICING_SOURCE,
            "fingerprint": get_pricing().fingerprint(),
        },
        # 已注册的工具（内置 + MCP）。开着 MCP 时会比关着多几个 ——
        # 这是判断"MCP 到底接上没有"最快的办法。
        "tools": registry.snapshot(),
    }


def _collect_metrics(response: RecommendationResponse):
    for name, result in response.agent_results.items():
        metrics_collector.record_agent_call(
            agent_name=name,
            success=result.success,
            latency_ms=result.latency_ms,
        )
    # 注意：这里读的是 response.harness（顶层字段），不是 agent_results 里的东西。
    # agent_results 会因为声明类型是基类而被 pydantic 截断子类字段，
    # 但 harness 是顶层声明的，能完整读到。
    if response.harness:
        metrics_collector.record_llm_usage(response.harness.usage.model_dump())


if __name__ == "__main__":
    # reload 改成显式开启（ECOM_DEV_RELOAD=true），默认关闭。
    #
    # 原先这里硬编码 reload=True，是一个真实的坑：uvicorn 的 reloader
    # 重启时会杀掉【整个子进程树】，而 MCP 的 stdio 子进程会先死 ——
    # 症状是静默挂起或 BrokenPipe，且没有任何栈指向真正原因
    # （CLAUDE.md 陷阱 5，也是 services/mcp_client.py 用短连接的原因）。
    # 容器里更是完全不需要热重载（Dockerfile 已直接起 uvicorn）。
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=settings.dev_reload)
