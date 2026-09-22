"""
Redis 实时特征存储的测试 —— 全部离线（用 conftest 里的 `FakeRedis`）。

守着的三条契约（见 `services/feature_store.py` 的模块 docstring）：

  1. 窗口靠 score 区间在【读取时】强制，TTL 只是 GC
  2. member = item_id，重复触达天然去重，且 score 是"最后触达"
  3. 永不抛异常；读的结果有三态：None（失败）/ {}（新用户）/ {...}（有数据）
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from services.feature_store import ALLOWED_BEHAVIOR_TYPES, FeatureStore

DAY = 86400
HOUR = 3600


def _store(fake: Any, **overrides: Any) -> FeatureStore:
    config: dict[str, Any] = dict(
        ttl=30 * DAY, window_days=30, timeout_s=0.5, tz_offset_hours=8
    )
    config.update(overrides)
    return FeatureStore(fake, **config)


# ── 窗口 ────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_events_inside_window_are_counted(fake_redis) -> None:
    store = _store(fake_redis)
    now = time.time()
    await store.backfill("u1", "view", "P001", now - 2 * HOUR)
    await store.backfill("u1", "view", "P003", now - 3 * DAY)
    await store.backfill("u1", "purchase", "P003", now - 5 * DAY, amount=1899.0)

    features = await store.get_features("u1")

    assert features is not None and features != {}
    assert features["view_count_7d"] == 2
    assert features["purchase_count_30d"] == 1
    assert features["avg_order_amount"] == 1899.0


@pytest.mark.anyio
async def test_events_outside_window_are_excluded(fake_redis) -> None:
    """10 天前的浏览不进 view_count_7d —— 这就是"滑动窗口"本身。"""
    store = _store(fake_redis)
    now = time.time()
    await store.backfill("u1", "view", "P001", now - 2 * HOUR)
    await store.backfill("u1", "view", "P011", now - 10 * DAY)

    features = await store.get_features("u1")

    assert features["view_count_7d"] == 1
    assert features["recent_views"] == ["P001"]


@pytest.mark.anyio
async def test_recent_lists_share_the_window_with_counts(fake_redis) -> None:
    """
    回归测试：列表和计数必须用【同一个窗口】。

    修之前列表取的是"最近 N 条"、不受窗口约束，于是实测出过一个自相矛盾的
    组合：`purchase_count_30d=0` 却 `avg_order_amount=5999`（客单价从一条
    45 天前的购买里算出来了）。模型看到那个矛盾只会照着编画像。
    """
    store = _store(fake_redis)
    now = time.time()
    await store.backfill("u1", "view", "P001", now - 2 * HOUR)
    await store.backfill("u1", "purchase", "P002", now - 45 * DAY, amount=5999.0)

    features = await store.get_features("u1")

    assert features["purchase_count_30d"] == 0
    assert features["recent_purchases"] == []
    assert features["avg_order_amount"] == 0.0  # 而不是 5999.0


# ── member / score 语义 ─────────────────────────────────────────


@pytest.mark.anyio
async def test_repeated_view_of_same_item_dedupes_and_refreshes(fake_redis) -> None:
    """
    member = item_id，所以同一商品看两次只算一次；
    第二次会把 score 推到最新 —— 也就是"最后触达"语义。
    """
    store = _store(fake_redis)
    now = time.time()
    await store.backfill("u1", "view", "P001", now - 3 * DAY)
    await store.backfill("u1", "view", "P001", now - 1 * HOUR)

    features = await store.get_features("u1")

    assert features["view_count_7d"] == 1
    assert fake_redis.zsets["fs:behavior:u1:view"]["P001"] == pytest.approx(
        now - HOUR
    )


@pytest.mark.anyio
async def test_write_prunes_members_beyond_ttl(fake_redis) -> None:
    """
    修剪必须真的发生。只靠 EXPIRE 不够 —— 每次写入都会刷新 key 的 TTL，
    活跃用户的 ZSET 会一直长下去（这是内存泄漏，不是"数据留久点"）。
    """
    store = _store(fake_redis, ttl=7 * DAY)
    now = time.time()
    await store.backfill("u1", "view", "OLD", now - 30 * DAY)

    await store.record_behavior("u1", "view", "NEW")

    assert "OLD" not in fake_redis.zsets["fs:behavior:u1:view"]
    assert "NEW" in fake_redis.zsets["fs:behavior:u1:view"]


@pytest.mark.anyio
async def test_backfill_never_moves_last_seen_backwards(fake_redis) -> None:
    """
    灌历史数据时到达顺序未必是时间顺序。last_seen 被旧事件推回去的话，
    `days_since_last_visit` 会凭空变大 —— 一个活跃用户被报成流失用户。
    """
    store = _store(fake_redis)
    now = time.time()
    await store.backfill("u1", "view", "P001", now - 1 * HOUR)
    await store.backfill("u1", "view", "P002", now - 100 * DAY)

    features = await store.get_features("u1")

    # 直接断言底层存的值：days_since_last_visit 保留 1 位小数，
    # 1 小时会被舍成 0.0，用它断言看不出"有没有被推回去"。
    assert float(fake_redis.strings["fs:last_seen:u1"]) == pytest.approx(
        now - HOUR
    )
    assert features["days_since_last_visit"] == 0.0, "不能被 100 天前的旧事件推回去"


# ── 三态 ────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_unknown_user_returns_empty_dict_not_none(fake_redis) -> None:
    """三态里的中间那态：读成功，但这个用户从没被上报过。"""
    store = _store(fake_redis)

    assert await store.get_features("nobody") == {}


@pytest.mark.anyio
async def test_missing_client_returns_none(fake_redis) -> None:
    """依赖没接上（开关关闭）—— 和"这个用户没数据"是两回事。"""
    store = _store(None)

    assert await store.get_features("u1") is None
    assert await store.record_behavior("u1", "view", "P001") is False


@pytest.mark.anyio
async def test_churn_user_is_not_reported_as_new(fake_redis) -> None:
    """
    窗口内为空、但历史存在 —— 必须给出 days_since_last_visit，
    否则"45 天前来过"和"从没来过"完全不可区分，而要的是两种策略。
    """
    store = _store(fake_redis)
    await store.backfill("u1", "view", "P002", time.time() - 45 * DAY)

    features = await store.get_features("u1")

    assert features != {}, "有历史就不该被当成新用户"
    assert features["view_count_7d"] == 0
    assert features["days_since_last_visit"] == pytest.approx(45.0, abs=0.1)


# ── 降级：永不抛 ─────────────────────────────────────────────────


@pytest.mark.anyio
async def test_read_failure_returns_none_and_does_not_raise(fake_redis) -> None:
    store = _store(fake_redis)
    fake_redis.raise_on = "zcount"

    assert await store.get_features("u1") is None


@pytest.mark.anyio
async def test_write_failure_returns_false_and_does_not_raise(fake_redis) -> None:
    store = _store(fake_redis)
    fake_redis.raise_on = "zadd"

    assert await store.record_behavior("u1", "view", "P001") is False


@pytest.mark.anyio
async def test_slow_read_times_out_instead_of_blocking(fake_redis) -> None:
    """Redis 卡住时必须在 timeout 内返回 None，不能拖住整条请求链路。"""

    class SlowRedis:
        async def zcount(self, *args: Any, **kwargs: Any) -> int:
            import asyncio

            await asyncio.sleep(5)
            return 0

    store = FeatureStore(SlowRedis(), timeout_s=0.05)
    started = time.perf_counter()
    result = await store.get_features("u1")
    elapsed = time.perf_counter() - started

    assert result is None
    assert elapsed < 1.0, f"没有在超时内返回，实际用了 {elapsed:.2f}s"


@pytest.mark.anyio
async def test_warmup_failure_does_not_raise(fake_redis) -> None:
    fake_redis.raise_on = "ping"

    assert await _store(fake_redis).warmup() is False
    assert await _store(fake_redis).warmup() is False  # 可重复调用


# ── 输入校验 / 键空间 ────────────────────────────────────────────


@pytest.mark.anyio
async def test_rejects_behavior_type_outside_whitelist(fake_redis) -> None:
    """
    类型名会拼进 key。白名单防的是键空间污染 ——
    传个 `amount` 或 `x:y` 进来就会和本模块自己的键撞上。
    """
    store = _store(fake_redis)

    assert await store.record_behavior("u1", "amount", "P001") is False
    assert await store.record_behavior("u1", "view:evil", "P001") is False
    assert fake_redis.zsets == {}
    assert ALLOWED_BEHAVIOR_TYPES == frozenset({"view", "purchase"})


@pytest.mark.anyio
async def test_empty_item_id_is_rejected(fake_redis) -> None:
    assert await _store(fake_redis).record_behavior("u1", "view", "") is False


@pytest.mark.anyio
async def test_clear_only_touches_its_own_keyspace(fake_redis) -> None:
    """`redis_url` 指向 db 0，是和别人共用的库 —— clear 不能删别人的键。"""
    store = _store(fake_redis)
    await store.record_behavior("u1", "view", "P001")
    fake_redis.strings["someone:elses:key"] = "keep me"

    removed = await store.clear()

    assert removed > 0
    assert fake_redis.zsets == {}
    assert fake_redis.strings.get("someone:elses:key") == "keep me"


# ── 时区 ────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_active_hours_uses_configured_timezone(fake_redis) -> None:
    """
    score 是 UTC epoch，"几点"是被解释出来的。必须显式固定偏移 ——
    Dockerfile 里没有 ENV TZ（容器是 UTC），不固定会得到差 8 小时的值，
    而 LLM 照样把它当"活跃时段"用。
    """
    score = time.time() - HOUR

    store_cst = _store(fake_redis, tz_offset_hours=8)
    await store_cst.backfill("u1", "view", "P001", score)
    expected_cst = datetime.fromtimestamp(
        score, timezone(timedelta(hours=8))
    ).hour
    assert (await store_cst.get_features("u1"))["active_hours"] == [expected_cst]

    fake_redis.zsets.clear()
    fake_redis.strings.clear()

    store_utc = _store(fake_redis, tz_offset_hours=0)
    await store_utc.backfill("u1", "view", "P001", score)
    expected_utc = datetime.fromtimestamp(score, timezone.utc).hour
    assert (await store_utc.get_features("u1"))["active_hours"] == [expected_utc]

    assert expected_cst != expected_utc, "两个时区算出来一样就说明时区没生效"
