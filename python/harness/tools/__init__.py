"""
工具层 —— 让"内置函数"和"MCP 工具"对调用方同构。

    这一层存在的理由（以及它不做什么）
    ────────────────────────────────
    做：把「调用 + 超时 + 重试 + 降级 + 记录」这套重复代码收敛到一处，
        并让 MCP 工具和本地函数长得一样。

    不做：不让模型决定主推荐链路该调什么。库存 Agent 仍然是
          `await self.db.batch_query_stock(...)` 这样写死的调用。

    唯一由模型自主选工具的地方是运营 Copilot（python/copilot/）。
    这条界线是刻意的：确定性场景不该引入 ReAct 的不确定性。
"""

from __future__ import annotations

from .registry import ToolRegistry
from .spec import ToolResult, ToolSpec

__all__ = ["ToolRegistry", "ToolResult", "ToolSpec", "build_registry"]


async def build_registry(*, include_mcp: bool = True) -> ToolRegistry:
    """
    组装一个注册表：内置工具 + （可选）MCP 工具。

    未装配 MCP 时（配置关闭 / Server 起不来）只返回内置工具 ——
    Copilot 照样能回答"系统现在健康吗"这类问题，只是不能查库存。
    这是刻意的降级：**少几个工具，而不是整个功能不可用**。
    """
    from config import get_settings

    from .builtin import build_builtin_tools

    registry = ToolRegistry()
    registry.register_many(build_builtin_tools())

    if include_mcp and get_settings().mcp_wms_enabled:
        from .mcp_source import build_mcp_tools

        registry.register_many(await build_mcp_tools("wms"))

    return registry
