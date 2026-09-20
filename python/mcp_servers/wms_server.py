"""
D2 —— WMS（仓储管理）MCP Server。

    stdio 传输，由本项目作为 MCP Client 调用（InventoryAgent -> mcp_client）。
    python mcp_servers/wms_server.py     # 正常由客户端拉起，一般不手动跑

    为什么自建而不是用现成的 SQLite MCP Server
    ─────────────────────────────────────────
    官方那个 mcp-server-sqlite 已在 2025-05-29 被移入 servers-archived 仓库，
    且存在 SQL 注入问题（CWE-89）、无只读模式，官方不再维护。

    更关键的是【语义不匹配】：它的工具形态是 read_query(sql) / write_query(sql)
    —— 一个通用 SQL 入口，意味着要由模型自己拼 SQL。而库存查询必须是
    【确定性】的：工具名与参数就固定了能力边界，模型没有拼 SQL 的空间。
    所以这不是重复造轮子，是现成方案在维护状态、安全性、语义匹配三个
    维度上都不合格。

    性能约束：本进程每次调用都会被重新拉起（见 PRD 决策 3：短连接，
    规避 uvicorn --reload 杀 stdio 子进程）。所以这里【只 import sqlite3 + mcp】，
    绝不 import agents/ 或 config/ —— 实测 import agents 会连带拉进 langchain，
    约 2 秒，那会变成每次调用的固定延迟。
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any

from mcp.server import MCPServer

PYTHON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_DB_PATH = "./wms.db"

server = MCPServer(
    name="wms",
    title="电商 WMS 库存服务",
    version="1.0.0",
    instructions=(
        "仓储管理系统的库存查询服务。提供单商品查库存、批量查库存、"
        "低库存清单三个只读工具，以及一个演示用的写工具 upsert_stock。"
        "库存数据以本服务为准，不要用商品目录里的库存字段做判断。"
    ),
)


def _db_path() -> str:
    """
    解析数据库路径。

    相对路径以【python/ 目录】为基准，而不是子进程的 cwd —— 客户端的 cwd
    是可控的（我们设成 python/），但把正确性寄托在调用方传对 cwd 上太脆。
    """
    raw = os.environ.get("ECOM_MCP_WMS_DB_PATH", DEFAULT_DB_PATH)
    if os.path.isabs(raw):
        return raw
    return os.path.normpath(os.path.join(PYTHON_DIR, raw))


def _connect() -> sqlite3.Connection:
    path = _db_path()
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"WMS 数据库不存在: {path}。先跑 python mcp_servers/init_wms_db.py"
        )
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "product_id": row["product_id"],
        "stock": row["stock"],
        "safety_threshold": row["safety_threshold"],
        "low_stock_threshold": row["low_stock_threshold"],
        "updated_at": row["updated_at"],
    }


def _level(stock: int, safety: int, low: int) -> str:
    if stock <= safety:
        return "critical"
    if stock <= low:
        return "warning"
    return "normal"


@server.tool(
    description="查询单个商品的实时库存。返回库存量、安全库存阈值、低库存阈值与更新时间。"
)
def query_stock(product_id: str) -> dict[str, Any]:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM wms_stock WHERE product_id = ?", (product_id,)
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        # 返回一个可判定的结果，而不是抛异常 —— 让调用方（包括模型）能自行处置
        return {"product_id": product_id, "found": False, "error": "product_not_found"}

    result = _row_to_dict(row)
    result["found"] = True
    result["level"] = _level(
        row["stock"], row["safety_threshold"], row["low_stock_threshold"]
    )
    return result


@server.tool(
    description=(
        "批量查询多个商品的实时库存，一次返回所有结果。"
        "推荐链路一次要查最多 30 个商品，用这个接口可以把 N 次往返降为 1 次。"
    )
)
def batch_query_stock(product_ids: list[str]) -> dict[str, int]:
    if not product_ids:
        return {}

    conn = _connect()
    try:
        placeholders = ",".join("?" * len(product_ids))
        rows = conn.execute(
            f"SELECT product_id, stock FROM wms_stock WHERE product_id IN ({placeholders})",
            list(product_ids),
        ).fetchall()
    finally:
        conn.close()

    found = {r["product_id"]: r["stock"] for r in rows}
    # 查不到的商品补 -1，让调用方能区分"库存为 0"和"这个商品不在 WMS 里"。
    # 直接省略会让调用方无法判断是缺货还是数据缺失。
    return {pid: found.get(pid, -1) for pid in product_ids}


@server.tool(
    description=(
        "列出库存低于阈值的商品清单，含预警级别（critical/warning）与建议动作。"
        "不传 threshold 时用每个商品自己配置的低库存阈值。"
    )
)
def list_low_stock(threshold: int | None = None) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        if threshold is None:
            rows = conn.execute(
                "SELECT * FROM wms_stock WHERE stock <= low_stock_threshold"
                " ORDER BY stock ASC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM wms_stock WHERE stock <= ? ORDER BY stock ASC",
                (threshold,),
            ).fetchall()
    finally:
        conn.close()

    out = []
    for row in rows:
        level = _level(row["stock"], row["safety_threshold"], row["low_stock_threshold"])
        out.append(
            {
                "product_id": row["product_id"],
                "stock": row["stock"],
                "level": level,
                "action": "urgent_restock" if level == "critical" else "plan_restock",
                "updated_at": row["updated_at"],
            }
        )
    return out


@server.tool(
    description=(
        "写入或更新某个商品的库存。仅用于演示和测试造数据 —— "
        "正常的库存变动应由 WMS 系统自己产生，不由本服务负责。"
    )
)
def upsert_stock(product_id: str, stock: int) -> dict[str, Any]:
    if stock < 0:
        return {"ok": False, "error": "stock_negative"}

    now = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO wms_stock (product_id, stock, updated_at)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(product_id) DO UPDATE SET stock = excluded.stock,"
            " updated_at = excluded.updated_at",
            (product_id, stock, now),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "product_id": product_id, "stock": stock, "updated_at": now}


@server.resource(
    "wms://stock/{product_id}",
    name="商品库存",
    description="以资源形式读取某个商品的库存快照（MCP 除工具外的第二种能力类型）。",
    mime_type="application/json",
)
def stock_resource(product_id: str) -> str:
    import json

    return json.dumps(query_stock(product_id), ensure_ascii=False)


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    sys.exit(main())
