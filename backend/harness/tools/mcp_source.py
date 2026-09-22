"""
把 MCP Server 的工具接进注册表。

    关键点：MCP 工具的 schema 不需要任何转换
    ──────────────────────────────────────
    MCP v2 的 `Tool` 对象长这样（字段全是 snake_case，v1 是 camelCase）：
        Tool(name=..., description=..., input_schema={...})

    而 `input_schema` 本身就是标准的 JSON Schema。也就是说
    **MCP 的工具形状和我们要的形状是同一个东西**，直接搬过来即可。

    这也正是"不需要 langchain-mcp-adapters"的依据 ——
    那个库是把 MCP 工具转成 LangChain Tool 对象，而 langchain-core 的
    convert_to_openai_function 原生就认 MCP 这个形状（已实测）。

    每次调用新起一个短连接
    ─────────────────────
    与 services/mcp_client.py 同样的理由：uvicorn --reload 会杀掉
    整个子进程树，任何 import 期建立的长连接都会先死，症状是
    【静默挂起】而不是报错。代价是每次约 1.15 秒。

    所以这里把 MCP 工具的默认超时设得比内置工具宽 —— 它天然就慢。
"""

from __future__ import annotations

import os
import sys
from typing import Any

import structlog
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from .spec import ToolSpec

logger = structlog.get_logger()

PYTHON_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
"""mcp_source.py 在 python/harness/tools/ 下，往上三层才是 python/。"""

_READ_ONLY = frozenset({"read_only"})

# 已知的 MCP Server。加新 Server 只需往这里加一项。
KNOWN_SERVERS: dict[str, str] = {
    "wms": "mcp_servers/wms_server.py",
}


def _server_params(server_key: str) -> StdioServerParameters:
    script = os.path.join(PYTHON_DIR, KNOWN_SERVERS[server_key])
    env = os.environ.copy()
    # 子进程读不到 .env（那是主进程里 pydantic-settings 读的）
    env.setdefault("ECOM_MCP_WMS_DB_PATH", os.environ.get("ECOM_MCP_WMS_DB_PATH", "./wms.db"))
    return StdioServerParameters(
        command=sys.executable,     # 必须是绝对路径：这台机器上 6 个目录叫 .venv
        args=[script],
        cwd=PYTHON_DIR,
        env=env,
    )


async def list_server_tools(server_key: str) -> list[Any]:
    """连一次、列出工具、断开。失败返回空列表（不抛）。"""
    try:
        async with Client(_server_params(server_key)) as client:
            result = await client.list_tools()
        return list(result.tools)
    except Exception as exc:
        logger.error("mcp.list_tools_failed", server=server_key,
                     error=str(exc)[:200], error_type=type(exc).__name__)
        return []


def _make_handler(server_key: str, tool_name: str, timeout_s: float, fallback):
    async def handler(**kwargs: Any) -> Any:
        async with Client(_server_params(server_key)) as client:
            result = await client.call_tool(
                tool_name, kwargs, read_timeout_seconds=timeout_s
            )
        if result.is_error:
            # 抛出去让注册表统一处理（重试 / 降级 / 记录）。
            # 这里不用 MCP 自己的异常类型，是为了让注册表只认通用的 Exception，
            # 不必知道 MCP 的存在。
            raise RuntimeError(f"mcp_tool_error: {_first_text(result)}")

        content = result.structured_content
        # MCP 要求结构化输出的顶层是 JSON 对象，所以返回【裸数组】的工具
        # 会被包成 {"result": [...]}。这里拆掉，让调用方拿到裸数组。
        if isinstance(content, dict) and set(content.keys()) == {"result"}:
            return content["result"]
        return content

    return handler


def _first_text(result: Any) -> str:
    try:
        return str(result.content[0].text)[:200]
    except Exception:
        return str(result.content)[:200]


async def build_mcp_tools(
    server_key: str,
    *,
    timeout_s: float = 6.0,
    expose_writes: bool = False,
) -> list[ToolSpec]:
    """
    把某个 MCP Server 的工具接成 ToolSpec。

    参数选择：
      timeout_s=6.0  —— 比内置工具宽，因为每次调用要起子进程（约 1.15 秒）
      expose_writes  —— 默认【不】暴露写操作。模型不该有能力改数据，
                        除非显式打开。

    工具名的处理：MCP 的工具名可能与内置工具重名。这里加了 server 前缀
    （`batch_query_stock` -> `wms__batch_query_stock`），
    让模型能区分"这是本地能力"还是"这是外部系统的"。前缀同时也会出现在
    调用日志里，排查时一眼能看出请求去了哪儿。
    """
    raw_tools = await list_server_tools(server_key)
    specs: list[ToolSpec] = []

    for t in raw_tools:
        is_write = not _is_read_only(t.name)
        if is_write and not expose_writes:
            logger.debug("mcp.tool_skipped_write", server=server_key, tool=t.name)
            continue

        tags = frozenset({"mcp", "write"} if is_write else {"mcp", "read_only"})
        specs.append(
            ToolSpec(
                name=f"{server_key}__{t.name}",
                description=f"[{server_key} 库存系统] {t.description or t.name}",
                input_schema=t.input_schema or {"type": "object", "properties": {}},
                handler=_make_handler(server_key, t.name, timeout_s, None),
                timeout_s=timeout_s,
                max_attempts=1,   # 每次尝试起一个子进程，重试代价高，不值得
                tags=tags,
                source=f"mcp:{server_key}",
            )
        )

    logger.info("mcp.tools_registered", server=server_key, count=len(specs))
    return specs


def _is_read_only(tool_name: str) -> bool:
    """
    按名字判断是不是写操作。

    为什么不问 Server：MCP 的 Tool 有 annotations 字段可以声明，
    但我们自己的 wms_server 没填。与其依赖一个可选字段，
    不如用一个显式的白名单 —— 漏判的后果是"写操作没被拦住"，
    所以这里【保守】处理：名字里没有明确只读语义的，一律当写操作。
    """
    return tool_name.startswith(("query_", "list_", "get_", "search_", "batch_query_"))
