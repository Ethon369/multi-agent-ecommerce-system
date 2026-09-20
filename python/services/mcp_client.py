"""
D4 —— WMS MCP 客户端。

    为什么每次调用都新起一个短连接，而不是保持长连接
    ────────────────────────────────────────────────
    `main.py` 用 uvicorn `--reload` 跑（开发时），reloader 重启时会杀掉整个
    子进程树。任何在 import 期建立的长连接都会先死掉，而症状是【静默挂起】
    或 `BrokenPipe` —— 没有任何栈能指向真正的原因。

    短连接的代价是每次约 100~300ms 的进程启动开销。对一次推荐只调用
    一次（batch_query_stock）来说完全可以接受，换来的是完全的可预测性。

    为什么 command 必须用 sys.executable 绝对路径
    ────────────────────────────────────────────
    写 "python" 会命中 PATH 上的任意解释器。这台机器上有 11 个解释器、
    其中 6 个叫同样的 `.venv`（已实测）—— 子进程会跑在错误的 venv 里，
    症状是 `ModuleNotFoundError: mcp` 之类，且极难定位。

    降级契约（PRD 决策 5）
    ──────────────────────
    本模块【永不抛异常】给调用方。任何失败都返回 None 并记一条结构化日志。
    调用方（InventoryAgent）据此回落到 Product.stock。
    MCP 是外挂依赖，不该成为主链路的单点故障。
"""

from __future__ import annotations

import os
import sys
from typing import Any

import structlog
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from config import get_settings

logger = structlog.get_logger()

PYTHON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WMS_SERVER_PATH = os.path.join(PYTHON_DIR, "mcp_servers", "wms_server.py")


class WMSMCPClient:
    """库存 WMS 的 MCP 客户端。所有方法失败时返回 None。"""

    def __init__(self, timeout_s: float | None = None) -> None:
        settings = get_settings()
        self.timeout_s = timeout_s if timeout_s is not None else settings.mcp_wms_timeout

    def _server_params(self) -> StdioServerParameters:
        env = os.environ.copy()
        # 子进程读不到 .env（那是 pydantic-settings 在主进程里读的），
        # 所以这里显式把库路径传下去。
        env["ECOM_MCP_WMS_DB_PATH"] = get_settings().mcp_wms_db_path
        # Windows 上子进程需要 SYSTEMROOT 才能正常启动 Python 运行时
        return StdioServerParameters(
            command=sys.executable,
            args=[WMS_SERVER_PATH],
            cwd=PYTHON_DIR,
            env=env,
        )

    async def _call(self, tool: str, arguments: dict[str, Any]) -> Any | None:
        """
        起一个短连接、调一次工具、拆掉。

        返回结构化结果；失败返回 None（并留下日志说明原因）。
        """
        import asyncio

        try:
            async with asyncio.timeout(self.timeout_s):
                async with Client(self._server_params()) as client:
                    result = await client.call_tool(tool, arguments)

            if result.is_error:
                logger.warning(
                    "mcp.tool_error", server="wms", tool=tool, arguments=arguments,
                    detail=str(result.content)[:200],
                )
                return None

            return result.structured_content

        except TimeoutError:
            # 库存查询是毫秒级操作，超时说明子进程已经异常。
            # 继续等只会拖慢主链路 —— 所以 mcp_wms_timeout 必须小于
            # agent_timeout_inventory，让 MCP 先超时、Agent 还有余量降级。
            logger.error(
                "mcp.timeout", server="wms", tool=tool, timeout_s=self.timeout_s
            )
            return None
        except Exception as exc:
            logger.error(
                "mcp.failed", server="wms", tool=tool,
                error=str(exc)[:200], error_type=type(exc).__name__,
            )
            return None

    # ── 对外接口 ────────────────────────────────────────────────

    async def batch_query_stock(self, product_ids: list[str]) -> dict[str, int] | None:
        """
        批量查库存。这是推荐链路里唯一被调用的方法。

        为什么必须有批量接口：一次推荐 P1 召回最多 num_items*2 个商品
        （默认 20，上限更高）。逐个 query_stock 意味着 N 次 stdio 往返 +
        N 次子进程启动（每次 100~300ms），批量接口把这个降为 1 次。
        这是【由实际调用模式倒推】出来的接口设计。
        """
        if not product_ids:
            return {}
        return await self._call("batch_query_stock", {"product_ids": list(product_ids)})

    async def query_stock(self, product_id: str) -> dict[str, Any] | None:
        return await self._call("query_stock", {"product_id": product_id})

    async def list_low_stock(self, threshold: int | None = None) -> list[dict[str, Any]] | None:
        args: dict[str, Any] = {}
        if threshold is not None:
            args["threshold"] = threshold
        data = await self._call("list_low_stock", args)
        if data is None:
            return None
        # MCP 要求结构化输出的顶层是 JSON 对象，所以返回裸数组的工具
        # 会被包成 {"result": [...]}。这里统一拆掉，让调用方拿到裸数组。
        return data.get("result", []) if isinstance(data, dict) else data

    async def read_stock_resource(self, product_id: str) -> str | None:
        """读 MCP 资源（除工具外的第二种能力类型）。"""
        import asyncio

        try:
            async with asyncio.timeout(self.timeout_s):
                async with Client(self._server_params()) as client:
                    result = await client.read_resource(f"wms://stock/{product_id}")
            return result.contents[0].text
        except Exception as exc:
            logger.error(
                "mcp.resource_failed", server="wms", uri=f"wms://stock/{product_id}",
                error=str(exc)[:200],
            )
            return None
