"""
灌用户行为种子数据到 Redis 特征库（一次性脚本）。

    python scripts/seed_behavior.py [--reset]

    ── 为什么需要它
    ──────────────
    "实时特征"要能演示，前提是库里【有】行为数据。而运行时没有任何东西
    会写行为事件（HTTP 上报端点属于下一轮），所以冷启动的库是空的 ——
    每个用户都会走 `redis_empty`，看起来像"功能没接上"。

    这个脚本补的就是这一环：一次性灌一批【确定性】的行为流，让
    `view_count_7d` / `purchase_count_30d` / `active_hours` /
    `days_since_last_visit` 都有真实的值可看。

    和 `mcp_servers/init_wms_db.py` 是同一个模式：
    显式跑一次、种子确定性、跑完自证。

    ── 为什么走 backfill 而不是 record_behavior
    ────────────────────────────────────────
    种子要跨越窗口边界（2 小时前 / 26 小时前 / 10 天前 / 45 天前），
    必须能指定历史时间戳。运行时路径【刻意】拿不到这个能力
    （理由见 FeatureStore.backfill 的 docstring），所以离线工具走 backfill。

    ── 三个用户，各证明一件事
    ──────────────────────
    u_seed_active  近 7 天有浏览、近 30 天有购买
                   -> 证明"窗口内有数据"时真的读得到（source=redis）
    u_seed_churn   45 天前来过，窗口内为空
                   -> 证明"窗口内为空但历史存在"不会被当成新用户 ——
                      他会拿到 days_since_last_visit=45，而不是写死的
                      "近 7 天浏览 25 次"
    u_seed_new     刻意【不灌】
                   -> 证明"从没被上报过"是第三种情况（source=redis_empty）

    第三个用户没有数据，所以它不在下面的 SEED_USERS 里 —— 它的"种子"
    就是不存在本身。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

PYTHON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PYTHON_DIR not in sys.path:
    sys.path.insert(0, PYTHON_DIR)

HOUR = 3600
DAY = 86400

#: 用户 -> [(行为类型, 商品ID, 多久以前, 金额)]
#:
#: 金额只对 purchase 有意义。这些偏移量是【相对当前时刻】的，
#: 所以结构确定（谁能进 7 天窗口、谁是流失用户），但具体时间点会变 ——
#: 这正是"滑动窗口"该有的样子。
SEED_USERS: dict[str, list[tuple[str, str, float, float | None]]] = {
    "u_seed_active": [
        ("view", "P001", 2 * HOUR, None),
        ("view", "P003", 26 * HOUR, None),
        ("view", "P005", 3 * DAY, None),
        ("view", "P011", 10 * DAY, None),
        ("view", "P015", 40 * DAY, None),
        ("purchase", "P003", 5 * DAY, 1899.0),
        ("purchase", "P007", 20 * DAY, 399.0),
    ],
    "u_seed_churn": [
        ("view", "P002", 45 * DAY, None),
        ("view", "P004", 50 * DAY, None),
        ("purchase", "P002", 45 * DAY, 5999.0),
    ],
}


async def seed(reset: bool) -> int:
    from config import get_settings

    settings = get_settings()
    if not settings.feature_store_enabled:
        # 不静默成功是有意的：写完却没人读，等于什么都没做。
        print("✗ ECOM_FEATURE_STORE_ENABLED 是 false —— 特征存储没接上。")
        print("  先在 python/.env 里设成 true，再跑本脚本。")
        return 1

    from harness.deps import get_feature_store

    store = get_feature_store()
    if store is None:
        print("✗ 特征存储构造失败（开关为真却拿到 None，这是个 bug）")
        return 1

    if reset:
        removed = await store.clear()
        print(f"已清空 {removed} 个已有的特征键")

    now = time.time()
    attempted = 0
    written = 0
    for user_id, events in SEED_USERS.items():
        for behavior_type, item_id, ago, amount in events:
            attempted += 1
            ok = await store.backfill(
                user_id, behavior_type, item_id, now - ago, amount
            )
            written += 1 if ok else 0

    print()
    print("行为种子已写入")
    print(f"  用户数         : {len(SEED_USERS)}")
    print(f"  事件数         : {written}/{attempted}")
    print()

    # ── 自证：用【消费端会看到的那份数据】读回来 ──
    # 这一步是整个脚本的重点。只打印"写了 N 条"证明不了任何事 ——
    # init_wms_db.py 也是同样的思路（它专门报 diverged_from_product_stock，
    # 而不是只报"写了 15 行"）。
    print("读回来（这就是 UserProfileAgent 会拿到的行为数据）:")
    for user_id in list(SEED_USERS) + ["u_seed_new"]:
        features = await store.get_features(user_id)
        if features is None:
            print(f"  {user_id:<16} 读取失败")
        elif not features:
            print(f"  {user_id:<16} 没有任何记录 -> source=redis_empty（全新用户）")
        else:
            print(
                f"  {user_id:<16} source=redis"
                f"  近7天浏览={features['view_count_7d']}"
                f"  近30天购买={features['purchase_count_30d']}"
                f"  客单价={features['avg_order_amount']}"
                f"  活跃时段={features['active_hours']}"
                f"  距今={features['days_since_last_visit']}天"
            )
            print(f"  {'':<16} 最近浏览={features['recent_views']}")

    print()
    print("预期：u_seed_active 近7天浏览=3；u_seed_churn 全为 0 但距今=45.0 天")
    print("      （后者是关键 —— 他不是新用户，不该被喂写死的演示数据）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="灌用户行为种子数据到 Redis 特征库")
    parser.add_argument("--reset", action="store_true", help="先清空已有的特征键")
    args = parser.parse_args()
    return asyncio.run(seed(args.reset))


if __name__ == "__main__":
    sys.exit(main())
