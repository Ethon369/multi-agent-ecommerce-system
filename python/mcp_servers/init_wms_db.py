"""
D3 —— WMS SQLite 建表 + 种子数据（一次性脚本）。

    python mcp_servers/init_wms_db.py [--db PATH] [--reset]

**种子数据是【故意】和 Product.stock 不一致的。**

为什么：验收项 A5 要能证明"推荐结果真的走了 MCP"，而不是"看起来一样所以
大概是通的"。如果 WMS 库存恰好等于 Product.stock，那么 MCP 通与不通的
输出完全相同 —— 测试通过了也说明不了任何事。所以这里确定性地制造差异：
有些商品 WMS 库存为 0（会被库存过滤掉）、有些是低库存、有些数值明显不同。

差异必须是【确定性】的，否则测试不可复现。

    本脚本可以承受重 import（一次性的）；wms_server.py 不行 ——
    它每次调用都起进程，多 2 秒 import 就是每次调用多 2 秒。
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone

PYTHON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PYTHON_DIR not in sys.path:
    sys.path.insert(0, PYTHON_DIR)

SCHEMA = """
CREATE TABLE IF NOT EXISTS wms_stock (
    product_id          TEXT    PRIMARY KEY,
    stock               INTEGER NOT NULL DEFAULT 0,
    safety_threshold    INTEGER NOT NULL DEFAULT 50,
    low_stock_threshold INTEGER NOT NULL DEFAULT 100,
    updated_at          TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wms_stock_stock ON wms_stock(stock);
"""


def seed_stock(product_id: str, catalog_stock: int) -> int:
    """
    由商品 ID 确定性地推导 WMS 库存，刻意与 Product.stock 不同。

    规则（按商品编号取模，可复现）：
        n % 7 == 0  ->  0            缺货，会被库存过滤掉。A5 靠这个产生可见差异
        n % 5 == 0  ->  目录库存的 1/10（至少 1）  触发 critical 预警
        n % 3 == 0  ->  目录库存 + 137            数值明显不同，但不触发预警
        其他        ->  与目录一致
    """
    n = int(product_id[1:])
    if n % 7 == 0:
        return 0
    if n % 5 == 0:
        return max(1, catalog_stock // 10)
    if n % 3 == 0:
        return catalog_stock + 137
    return catalog_stock


def init_db(db_path: str, reset: bool = False) -> dict:
    """建表并写入种子数据。返回统计信息。"""
    # 阈值沿用 inventory_agent.py:16-18 已有的常量，不另造一套。
    # 差别在于：这里把它们【存进表】，所以不同商品可以有不同阈值，
    # 改阈值不用发版 —— 这是相对原实现的一个实质改进。
    from agents.inventory_agent import LOW_STOCK_THRESHOLD, SAFETY_STOCK_THRESHOLD
    from agents.product_rec_agent import MOCK_PRODUCTS

    if reset and os.path.exists(db_path):
        os.remove(db_path)

    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        rows = [
            (
                p.product_id,
                seed_stock(p.product_id, p.stock),
                SAFETY_STOCK_THRESHOLD,
                LOW_STOCK_THRESHOLD,
                now,
            )
            for p in MOCK_PRODUCTS
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO wms_stock"
            " (product_id, stock, safety_threshold, low_stock_threshold, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()

        diverged = sum(
            1 for p in MOCK_PRODUCTS if seed_stock(p.product_id, p.stock) != p.stock
        )
        out_of_stock = sum(1 for p in MOCK_PRODUCTS if seed_stock(p.product_id, p.stock) == 0)
    finally:
        conn.close()

    return {
        "db_path": db_path,
        "products": len(MOCK_PRODUCTS),
        "diverged_from_product_stock": diverged,
        "out_of_stock_in_wms": out_of_stock,
    }


def main() -> int:
    from config import get_settings

    settings = get_settings()
    parser = argparse.ArgumentParser(description="初始化 WMS SQLite 库存库")
    parser.add_argument("--db", default=settings.mcp_wms_db_path,
                        help="数据库路径（默认取 ECOM_MCP_WMS_DB_PATH）")
    parser.add_argument("--reset", action="store_true", help="先删除已有文件再重建")
    args = parser.parse_args()

    stats = init_db(args.db, reset=args.reset)

    print("WMS 数据库已初始化")
    print(f"  路径           : {stats['db_path']}")
    print(f"  商品数         : {stats['products']}")
    print(f"  与目录库存不同 : {stats['diverged_from_product_stock']} 个"
          f"   <- A5 靠这批产生可见差异")
    print(f"  WMS 缺货       : {stats['out_of_stock_in_wms']} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
