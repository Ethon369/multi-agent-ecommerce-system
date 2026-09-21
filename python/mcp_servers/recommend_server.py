"""
D1 —— 推荐能力 MCP Server（把项目【开放出去】）。

    与 wms_server 的角色正好相反
    ──────────────────────────
    wms_server：本项目作为 Client 【消费】它，引入一个真实的外部数据源。
    recommend_server：本项目作为 Server 【暴露】能力，让外部 Host
                      （Claude Desktop / Cursor / 任何支持 MCP 的工具）能直接调。

    两者合起来才是「MCP 双侧」。

    不重复实现业务逻辑
    ─────────────────
    这里【只做协议壳】：所有工具都是 import 复用已有代码
    （通过 harness/deps.py 拿共享单例），不新写任何业务逻辑。
    这样 REST 接口和 MCP 接口看到的是同一份数据，不会出现"两套真相"。

    启动方式（由 Host 拉起，一般不手动跑）
    ────────────────────────────────────
        python mcp_servers/recommend_server.py

    Host 配置示例（Claude Desktop 的 claude_desktop_config.json）：
        {
          "mcpServers": {
            "ecommerce-recommend": {
              "command": "<python/.venv/Scripts/python.exe 的绝对路径>",
              "args": ["<repo>/python/mcp_servers/recommend_server.py"],
              "cwd": "<repo>/python"
            }
          }
        }
    ⚠️ command 必须写绝对路径 —— 这台机器上有 11 个解释器、其中 6 个目录叫 .venv，
       写 "python" 会命中任意一个，症状是 ModuleNotFoundError 且极难定位。
"""

from __future__ import annotations

import os
import sys
from typing import Any

# 这个文件要被【外部 Host】以独立进程拉起，cwd 不一定是 python/，
# 所以显式把 python/ 加进 sys.path，否则 import config / agents 都会失败。
PYTHON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PYTHON_DIR not in sys.path:
    sys.path.insert(0, PYTHON_DIR)

from mcp.server import MCPServer  # noqa: E402

server = MCPServer(
    name="ecommerce-recommend",
    title="电商多 Agent 推荐系统",
    version="1.0.0",
    instructions=(
        "本服务提供一个多 Agent 电商推荐系统的能力：个性化推荐、A/B 实验状态、"
        "系统运行指标。\n\n"
        "重要：recommend_products 会真实调用大模型，**单次约 5-15 秒**，"
        "请预留足够超时时间，不要当成毫秒级的本地查询。\n\n"
        "除 record_experiment_outcome 外，全部工具都是只读的。"
    ),
)


def _flat_response(payload: dict[str, Any]) -> dict[str, Any]:
    """
    把推荐响应压成扁平结构。

        为什么不直接返回 RecommendationResponse.model_dump()
        ─────────────────────────────────────────────────────
        理由不再是"绕开 pydantic 截断"—— 那个 bug 已于 2026-09-21 修复
        （agent_results 改用 SerializeAsAny，子类字段不再被吞）。

        现行理由：这个结构是喂给 Host 侧【模型】的。
        - 信息全在顶层，模型不用猜嵌套
        - 顺带挡掉 latency / confidence 这类运维细节，省 token
    """
    return {
        "request_id": payload.get("request_id"),
        "user_id": payload.get("user_id"),
        "products": [
            {
                "product_id": p.get("product_id"),
                "name": p.get("name"),
                "category": p.get("category"),
                "price": p.get("price"),
                "tags": p.get("tags", []),
            }
            for p in payload.get("products", [])
        ],
        "marketing_copies": payload.get("marketing_copies", []),
        "experiment_group": payload.get("experiment_group"),
        "total_latency_ms": round(payload.get("total_latency_ms", 0), 1),
        "usage": (payload.get("harness") or {}).get("usage"),
        "agents": (payload.get("harness") or {}).get("agents"),
    }


@server.tool(
    description=(
        "为用户生成个性化商品推荐，含营销文案与 A/B 实验分组。"
        "会真实调用大模型，单次约 5-15 秒 —— 请不要用默认的短超时。"
    )
)
async def recommend_products(
    user_id: str,
    scene: str = "homepage",
    num_items: int = 5,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from harness.deps import get_supervisor
    from models.schemas import RecommendationRequest

    if num_items < 1:
        return {"ok": False, "error": "num_items_must_be_positive"}
    # 上限不是安全限制而是成本限制：num_items 直接决定召回量，
    # 而每次重排都要调一次 LLM。
    if num_items > 20:
        return {"ok": False, "error": "num_items_too_large", "max": 20}

    request = RecommendationRequest(
        user_id=user_id, scene=scene, num_items=num_items, context=context or {}
    )
    response = await get_supervisor().recommend(request)
    return _flat_response(response.model_dump())


@server.tool(
    description=(
        "查看所有 A/B 实验的分组配置与统计结果（各组成功/失败次数、"
        "Thompson 采样的后验统计）。用于回答『实验跑得怎么样』『哪个组更好』。"
    )
)
async def get_experiments() -> dict[str, Any]:
    from harness.deps import get_ab_engine

    engine = get_ab_engine()
    out: dict[str, Any] = {}
    for exp_id, exp in engine.experiments.items():
        out[exp_id] = {
            "name": exp.name,
            "enabled": exp.enabled,
            "groups": [
                {
                    "name": g.name,
                    "weight": g.weight,
                    "config": g.config,
                    "successes": g.successes,
                    "failures": g.failures,
                }
                for g in exp.groups
            ],
            "stats": engine.get_stats(exp_id),
        }
    return out


@server.tool(
    description=(
        "查看系统运行指标：各 Agent 的调用次数/成功率/平均延迟、"
        "熔断器状态、LLM token 消耗与成本。用于回答『系统健康吗』『花了多少钱』。"
    )
)
async def get_metrics() -> dict[str, Any]:
    from harness import get_runtime
    from harness.deps import get_metrics_collector

    return {
        "agents": get_metrics_collector().get_agent_stats(),
        "llm": get_metrics_collector().get_llm_stats(),
        "breakers": get_runtime().snapshot(),
    }


@server.tool(
    description=(
        "记录一次 A/B 实验的结果（成功或失败），用于更新 Thompson 采样的后验分布。"
        "这是本服务【唯一】的写操作，且是幂等安全的（只累加计数）。"
    )
)
async def record_experiment_outcome(
    experiment_id: str, group: str, success: bool
) -> dict[str, Any]:
    from harness.deps import get_ab_engine

    engine = get_ab_engine()
    if experiment_id not in engine.experiments:
        # 返回可判定的结果而不是抛异常 —— 让调用方（包括模型）能自行处置
        return {
            "ok": False,
            "error": "experiment_not_found",
            "available": sorted(engine.experiments),
        }

    engine.record_outcome(experiment_id, group, success)
    return {"ok": True, "experiment_id": experiment_id, "group": group, "success": success}


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    sys.exit(main())
