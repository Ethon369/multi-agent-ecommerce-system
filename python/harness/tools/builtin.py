"""
内置工具 —— 把项目已有的能力包装成 ToolSpec。

    设计原则：只包装【已经有了】的能力，不新增业务逻辑。
    get_metrics / get_experiments 都是直接复用 main.py 里已经在用的那些对象
    （通过 harness/deps.py 拿共享单例），所以运营 Copilot 看到的数据
    和 REST 接口看到的是同一份，不会出现"两套真相"。

    description 的写法
    ─────────────────
    这些描述会原样送进模型的工具列表，模型靠它决定该不该调。
    所以要点明：① 这个工具能回答什么问题 ② 什么时候该用它。
    写得含糊，模型就会乱调或者不调。
"""

from __future__ import annotations

from typing import Any

from .spec import ToolSpec

_READ_ONLY = frozenset({"read_only"})


async def _get_metrics() -> dict[str, Any]:
    from ..deps import get_metrics_collector

    return {
        "agents": get_metrics_collector().get_agent_stats(),
        "llm": get_metrics_collector().get_llm_stats(),
    }


async def _get_experiments() -> dict[str, Any]:
    from ..deps import get_ab_engine

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
                    "successes": g.successes,
                    "failures": g.failures,
                }
                for g in exp.groups
            ],
            "stats": engine.get_stats(exp_id),
        }
    return out


async def _list_products(category: str | None = None) -> list[dict[str, Any]]:
    """
    列出商品目录。

    注意这里【不含实时库存】—— 库存要通过 MCP 的库存工具查。
    拆开的理由：目录是静态的（本地就能答），库存是动态的（要问外部系统）。
    合成一个工具会让模型分不清"这个商品的库存是真是假"。
    """
    from agents.product_rec_agent import MOCK_PRODUCTS

    rows = MOCK_PRODUCTS
    if category:
        rows = [p for p in rows if p.category == category]
    return [
        {
            "product_id": p.product_id,
            "name": p.name,
            "category": p.category,
            "price": p.price,
            "tags": list(p.tags),
        }
        for p in rows
    ]


def build_builtin_tools() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="get_metrics",
            description=(
                "查看本系统各 Agent 的运行指标（调用次数、成功率、平均延迟）"
                "以及 LLM token 消耗与成本。用于回答『系统现在健康吗』"
                "『哪个 Agent 慢』『花了多少钱』这类问题。"
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            handler=_get_metrics,
            timeout_s=3.0,
            tags=_READ_ONLY,
            source="builtin",
        ),
        ToolSpec(
            name="get_experiments",
            description=(
                "查看 A/B 实验的分组配置与统计结果（各组的成功/失败次数、"
                "Thompson 采样的后验统计）。用于回答『实验跑得怎么样』"
                "『哪个组效果更好』。"
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            handler=_get_experiments,
            timeout_s=3.0,
            tags=_READ_ONLY,
            source="builtin",
        ),
        ToolSpec(
            name="list_products",
            description=(
                "列出商品目录（商品 ID、名称、类目、价格、标签）。"
                "这是【静态目录】，不含实时库存 —— 要查某个商品还有多少货，"
                "请用库存查询工具。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "category": {
                        "type": ["string", "null"],
                        "description": "只列某个类目，如『耳机』。不传则列全部。",
                    }
                },
                "required": [],
            },
            handler=_list_products,
            timeout_s=3.0,
            tags=_READ_ONLY,
            source="builtin",
        ),
    ]
