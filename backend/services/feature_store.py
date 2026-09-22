"""
Redis 实时特征存储 —— 用户行为序列 + 滑动窗口聚合。

    它为什么被重写
    ──────────────
    这个文件原先有 116 行，但【从来没有被实例化过】：全仓库没有一处
    `FeatureStore(...)`，agent 里那个 `self.feature_store = None` 永远是 None，
    连 `redis` 包都没被 import 过。

    一份没跑过的代码等于没有 —— 而且它确实攒了一堆没被发现的问题：

      1. 产出的 key 集合和消费端（`user_profile_agent._collect_behavior`）
         的 7 个 key 对不上。直接换上去会让 prompt 里 4 个字段凭空消失、
         同时多出 5 个 LLM 没被告知含义的字段
      2. `record_behavior` 调了两次 `time.time()` —— payload 里的 ts 和
         zset 的 score 是两个不同的瞬间，窗口过滤和 RFM 用了两个时间源
      3. 金额从 `p.get("amount", 100)` 取，但写入的 payload 里根本没有
         `amount` —— monetary 恒等于一个魔数算出来的值
      4. 用 `len(窗口内全部成员)` 代替 `ZCOUNT` —— O(N) 全量回传客户端
      5. 把 TTL 当窗口用：`EXPIRE` 每次写入都刷新，活跃用户的老成员
         **永不过期**，ZSET 无界增长（这是内存泄漏，不是"数据留久点"）
      6. 完全没有 try/except —— Redis 一抖动就穿透到请求链路，接口直接 500
      7. 定义了 `logger` 但一次没用；`_compute_rfm` 的 `user_id` 参数没用

    这一版逐条修掉，并定下三条契约。

    契约一：窗口靠 score 区间在【读取时】强制
    ────────────────────────────────────────
    TTL 只是 GC，不是正确性机制。所有窗口（7d / 30d）都是读取时用
    `ZCOUNT` / `ZREVRANGE` / `ZRANGEBYSCORE` 的 score 区间算出来的。

    契约二：member = item_id，score = 服务端生成的时间戳
    ────────────────────────────────────────────────────
    同一商品重复触达只更新 score（天然去重），"最近看过什么"是真的最近，
    而不是"最近写入过什么"。

    时间戳【必须服务端生成】，不接受调用方传入。两个理由：
      - 本机 Redis 是 5.0，没有 `ZADD GT`（6.2+ 才有），拿不到"更大才更新"
        的服务端语义，只能由调用方保证单调递增 —— 所以时间源必须可信
      - 单位混淆是静默的：JS 的 `Date.now()` 是毫秒，差 1000 倍，
        窗口会恒为空。而窗口恒为空意味着"这个用户没有行为" ——
        一个输入单位的错误会静默变成一次对用户行为的编造

    契约三：永不抛异常给调用方
    ────────────────────────
    和 `services/mcp_client.py` 同一个口径。任何失败：
      - 写：记一条 warning 后返回 False
      - 读：返回 None（= 读失败）
    读取成功但该用户没有任何记录，返回的是【空 dict】而不是 None ——
    "Redis 挂了"和"这个用户是新的"是两件事，必须能分开。
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

logger = structlog.get_logger()

#: 允许的行为类型白名单。
#:
#: 不接受的不是"未知类型"本身，而是它能造成什么：类型名会拼进 key，
#: 一个含 `:` 的类型（或 `amount`、`last_seen` 这种内部后缀）会污染键空间、
#: 和本模块自己的其他 key 撞上。redis_url 指向 db 0，是和别人共用的。
ALLOWED_BEHAVIOR_TYPES = frozenset({"view", "purchase"})

#: 所有键的统一前缀。
_KEY_PREFIX = "fs"

#: `recent_views` 最多返回多少个商品 ID。
MAX_RECENT_VIEWS = 20

#: `recent_purchases` 最多返回多少个商品 ID。
MAX_RECENT_PURCHASES = 10


def _behavior_key(user_id: str, behavior_type: str) -> str:
    return f"{_KEY_PREFIX}:behavior:{user_id}:{behavior_type}"


def _amount_key(user_id: str) -> str:
    return f"{_KEY_PREFIX}:amount:{user_id}"


def _last_seen_key(user_id: str) -> str:
    return f"{_KEY_PREFIX}:last_seen:{user_id}"


def _top_hours(withscores: list[tuple[str, float]], tz: timezone) -> list[int]:
    """
    近 7 天"最后触达"小时 top3（按给定时区解释）。

    调用方必须已经把 score 限定在窗口内 —— 否则 29 天前 20:00 的一次触达
    今天还在给 20 点投票，直方图会系统性偏向过去。

    排序用 `(-次数, 小时)` 而不是 `Counter.most_common`：后者在并列时
    按插入顺序返回，同样的数据可能给出不同的 top3，测试会随机飘。
    """
    counter: Counter[int] = Counter()
    for _member, score in withscores:
        counter[datetime.fromtimestamp(score, tz).hour] += 1
    ranked = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    return [hour for hour, _n in ranked[:3]]


class FeatureStore:
    """
    Redis 实时特征存储。

    所有方法都遵守契约三：不抛异常。
    `redis_client` 为 None 时，写直接返回 False、读直接返回 None ——
    等于"这个依赖没接上"，而不是"这个用户没数据"。
    """

    def __init__(
        self,
        redis_client: Any = None,
        *,
        ttl: int = 2_592_000,
        window_days: int = 30,
        timeout_s: float = 0.5,
        tz_offset_hours: int = 8,
    ):
        self.redis = redis_client
        self.ttl = ttl
        self.window_days = window_days
        self.timeout_s = timeout_s
        # 固定偏移而不是 ZoneInfo("Asia/Shanghai")：Windows 上没有系统 tz
        # 数据库，得靠 tzdata 包 —— 而它在本项目里只是传递依赖，没写进
        # requirements.txt，随时可能消失。中国无夏令时，固定偏移是对的。
        self.tz = timezone(timedelta(hours=tz_offset_hours))

    # ---------- 写 ----------

    async def record_behavior(
        self,
        user_id: str,
        behavior_type: str,
        item_id: str,
        amount: float | None = None,
    ) -> bool:
        """
        记一次行为。返回是否写入成功。

        没有 `ts` 参数是有意的，理由见模块 docstring 契约二。

        `amount` 只在 purchase 时有意义，记进一个伴生的 hash ——
        Sorted Set 的 member 是 item_id，塞不下第二个值。

        刻意【不】用 pipeline：本项目一次推荐最多写几条行为，
        省下的几次往返换不来可读性和可测性（假 Redis 也要跟着实现 pipeline）。
        """
        if self.redis is None:
            return False
        if behavior_type not in ALLOWED_BEHAVIOR_TYPES:
            logger.warning(
                "feature_store.rejected", reason="bad_behavior_type",
                behavior_type=behavior_type, user_id=user_id,
            )
            return False
        if not item_id:
            return False

        # 只调一次 time.time()：分数和 last_seen 必须是同一个瞬间。
        now = time.time()
        key = _behavior_key(user_id, behavior_type)
        cutoff = now - self.ttl

        try:
            async with asyncio.timeout(self.timeout_s):
                await self.redis.zadd(key, {item_id: now})
                # 先写再剪。不修剪的话 EXPIRE 永远轮不到生效 ——
                # 每次写入都会把整个 key 的 TTL 刷新一遍，
                # 活跃用户的 ZSET 会一直长下去。
                await self.redis.zremrangebyscore(key, "-inf", cutoff)
                await self.redis.expire(key, self.ttl)
                await self.redis.set(_last_seen_key(user_id), now, ex=self.ttl)
                if amount is not None:
                    await self.redis.hset(
                        _amount_key(user_id), item_id, float(amount)
                    )
                    await self.redis.expire(_amount_key(user_id), self.ttl)
        except Exception as exc:
            logger.warning(
                "feature_store.record_failed", user_id=user_id,
                behavior_type=behavior_type, error=str(exc)[:200],
                error_type=type(exc).__name__,
            )
            return False

        return True

    async def backfill(
        self,
        user_id: str,
        behavior_type: str,
        item_id: str,
        at: float,
        amount: float | None = None,
    ) -> bool:
        """
        写入一条【历史】行为（显式指定时间戳）。

        为什么单独开一个方法，而不是给 `record_behavior` 加个可选 `ts` 参数：
        运行时路径【必须】拿不到"指定时间戳"这个能力。调用方给出毫秒时间戳
        （JS 的 `Date.now()` 是经典错误）会让 score 差 1000 倍、窗口恒为空 ——
        而空窗口会被解读成"这个用户没有行为"，于是一个单位的错误
        静默变成了一次对用户行为的编造。把能力隔到另一个方法上，
        运行时那条路就永远走不到它。

        本方法只给【离线工具】用：`scripts/seed_behavior.py` 灌种子、
        以及将来真的做数据导入时。它不参与请求链路。
        """
        if self.redis is None:
            return False
        if behavior_type not in ALLOWED_BEHAVIOR_TYPES:
            logger.warning(
                "feature_store.rejected", reason="bad_behavior_type",
                behavior_type=behavior_type, user_id=user_id,
            )
            return False
        if not item_id:
            return False

        key = _behavior_key(user_id, behavior_type)
        try:
            async with asyncio.timeout(self.timeout_s):
                await self.redis.zadd(key, {item_id: at})
                await self.redis.expire(key, self.ttl)
                if amount is not None:
                    await self.redis.hset(
                        _amount_key(user_id), item_id, float(amount)
                    )
                    await self.redis.expire(_amount_key(user_id), self.ttl)
                # last_seen 只在【更晚】时才前进 —— 灌历史数据时
                # 到达顺序未必是时间顺序，直接覆盖会让它倒退。
                current = await self.redis.get(_last_seen_key(user_id))
                if current is None or float(current) < at:
                    await self.redis.set(_last_seen_key(user_id), at, ex=self.ttl)
        except Exception as exc:
            logger.warning(
                "feature_store.backfill_failed", user_id=user_id, item_id=item_id,
                error=str(exc)[:200], error_type=type(exc).__name__,
            )
            return False

        return True

    async def clear(self) -> int:
        """
        删掉本模块写过的所有键。返回删除条数。

        只给【离线工具和测试】用。运行时没有"清空用户特征"这个需求，
        而一个能清库的方法挂在请求链路上是危险的。

        用 `SCAN` 而不是 `KEYS`：`KEYS` 会阻塞整个 Redis 实例，
        在真实库上是事故级的。
        """
        if self.redis is None:
            return 0
        removed = 0
        try:
            # 清库比单次读写慢得多，超时放宽 20 倍
            async with asyncio.timeout(self.timeout_s * 20):
                async for key in self.redis.scan_iter(match=f"{_KEY_PREFIX}:*"):
                    await self.redis.delete(key)
                    removed += 1
        except Exception as exc:
            logger.warning(
                "feature_store.clear_failed", error=str(exc)[:200],
                removed_before_failure=removed,
            )
        return removed

    async def warmup(self) -> bool:
        """
        预热连接：发一次 PING 把连接建起来。

            为什么需要它（实测踩的坑）
            ────────────────────────
            这台机器上【第一次连接】的代价约 **2 秒**，而之后每条命令只要
            **0.3 毫秒**。原因不是 Redis 慢：`localhost` 会先解析到 IPv6 的
            `::1`，而本机 Redis 只监听 IPv4 —— 连接先在 `::1` 上挂约 2 秒
            才回落到 `127.0.0.1`。实测对比：

                redis://localhost:6379/0   首次 PING  2052.3 ms
                redis://127.0.0.1:6379/0   首次 PING     2.4 ms

            后果不是"慢一点"，而是【永久降级】：请求路径上的超时是 0.5 秒，
            它正好卡在连接建立中间把连接掐断 —— 于是每次请求都重新发起连接、
            每次都超时，`source` 永远是 `fallback`，看起来像"功能没接上"。

            所以启动时在这里把这一次代价付掉。失败只记日志、不阻断启动 ——
            Redis 是可选依赖，它不在不该让整个应用起不来。
        """
        # 预热给的预算比请求路径宽：这里阻塞的是【启动】，不是用户请求。
        return await self._ping(self.timeout_s * 10, event="feature_store.warmup")

    async def ping(self) -> bool:
        """
        探活：带【请求路径】预算的 PING。`/ready` 用它。

            为什么和 warmup() 分开
            ────────────────────
            warmup 在启动时调一次，预算给到 `timeout_s * 10` —— 那段时间
            阻塞的是启动流程，慢一点没人在等。而 `/ready` 会被编排系统
            每几秒打一次：用 5 秒预算会让探针本身变成负载，Redis 挂掉时
            还会把探针请求堆起来。

            所以这里用请求路径的预算（默认 0.5s）。

        同 warmup：**永不抛异常**。
        就绪探针因为一个可选依赖不可用而返回 500，是最糟的形态 ——
        它会让编排系统把一个其实还在正常降级提供服务的实例摘掉。
        """
        return await self._ping(self.timeout_s, event="feature_store.ping")

    async def _ping(self, timeout_s: float, *, event: str) -> bool:
        """
        warmup 与 ping 共用的实现。

        抽出来的原因很直接：两者只差【预算】和【日志事件名】，
        而那正是它们被分成两个方法的原因 —— 逻辑本身没有理由写两遍。
        """
        if self.redis is None:
            return False
        started = time.perf_counter()
        try:
            async with asyncio.timeout(timeout_s):
                await self.redis.ping()
        except Exception as exc:
            logger.warning(
                f"{event}_failed", error=str(exc)[:200],
                error_type=type(exc).__name__,
                note="请求路径会走 fallback，不阻断",
            )
            return False
        logger.info(
            f"{event}_ok",
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return True

    # ---------- 读 ----------

    async def get_features(self, user_id: str) -> dict[str, Any] | None:
        """
        读这个用户的窗口内特征。

        返回值有三种，消费端靠它区分三种情况：

            None   —— 读取失败（连接异常 / 超时 / 依赖没接上）
            {}     —— 读成功，但这个用户【从没有任何记录】（全新用户）
            {...}  —— 读成功且有记录

        返回值里的【每一个字段都受窗口约束】，包括 recent_views /
        recent_purchases 两个列表 —— 它们和对应的计数用的是同一个窗口。
        不这样对齐就会出现自相矛盾的组合（"近 30 天购买 0 次" +
        "客单价 5999"），而模型会照着这个矛盾去编画像。

        第三种里一定带 `days_since_last_visit`，它可能是 45 这种大数 ——
        那是流失用户，不是新用户。这两种人在窗口计数下完全一样
        （都是 0），却要完全不同的策略，所以必须单独留一个信号。
        """
        if self.redis is None:
            return None

        now = time.time()
        view_key = _behavior_key(user_id, "view")
        purchase_key = _behavior_key(user_id, "purchase")
        seven_days_ago = now - 7 * 86400
        window_start = now - self.window_days * 86400

        try:
            async with asyncio.timeout(self.timeout_s):
                view_count_7d = await self.redis.zcount(
                    view_key, seven_days_ago, "+inf"
                )
                purchase_count_30d = await self.redis.zcount(
                    purchase_key, window_start, "+inf"
                )
                # 列表必须和上面的计数【用同一个窗口】，否则会自相矛盾：
                # 实测过一个流失用户，purchase_count_30d=0 却 avg_order_amount=5999
                # —— 因为列表取的是"最近 N 条"、不受窗口约束，
                # 客单价就从一条 45 天前的购买里算出来了。
                # 模型看到"30 天买 0 次、客单价 5999"只会困惑。
                recent_views = await self.redis.zrevrangebyscore(
                    view_key, "+inf", seven_days_ago,
                    start=0, num=MAX_RECENT_VIEWS,
                )
                recent_purchases = await self.redis.zrevrangebyscore(
                    purchase_key, "+inf", window_start,
                    start=0, num=MAX_RECENT_PURCHASES,
                )
                amounts = await self.redis.hgetall(_amount_key(user_id))
                last_seen_raw = await self.redis.get(_last_seen_key(user_id))
                # active_hours 需要 score，所以单独取一次带分数的窗口。
                # 升序返回，这里只用来算小时直方图，顺序无所谓。
                view_window = await self.redis.zrangebyscore(
                    view_key, seven_days_ago, "+inf", withscores=True
                )
        except Exception as exc:
            logger.error(
                "feature_store.read_failed", user_id=user_id,
                error=str(exc)[:200], error_type=type(exc).__name__,
            )
            return None

        if last_seen_raw is None:
            # 这个用户从来没有被上报过任何行为 —— 全新用户。
            # 这里【不】回落内置兜底值：替用户编造行为不是降级，是造假。
            # 冷启动用户该走冷启动分支，而不是被喂一个假的"活跃"画像。
            logger.info("feature_store.unknown_user", user_id=user_id)
            return {}

        last_seen = float(last_seen_raw)
        # 用 max(0, ...) 兜一下时钟回拨：如果 now 比 last_seen 小，
        # 会算出负数天数，喂给模型很难解释。
        days_since_last_visit = round(max(0.0, (now - last_seen) / 86400), 1)

        logger.info(
            "feature_store.read_ok", user_id=user_id,
            view_count_7d=int(view_count_7d),
            purchase_count_30d=int(purchase_count_30d),
            days_since_last_visit=days_since_last_visit,
        )

        return {
            "user_id": user_id,
            "recent_views": list(recent_views),
            "recent_purchases": list(recent_purchases),
            "view_count_7d": int(view_count_7d),
            "purchase_count_30d": int(purchase_count_30d),
            "avg_order_amount": self._avg_order_amount(amounts, recent_purchases),
            "active_hours": _top_hours(view_window, self.tz),
            "days_since_last_visit": days_since_last_visit,
        }

    @staticmethod
    def _avg_order_amount(amounts: dict[str, Any], purchased: list[str]) -> float:
        """
        已购商品的客单价。

        只对【有金额记录】的商品求平均。一个都没记到就返回 0.0 ——
        不是"平均 0 元"，而是"没有金额数据"，这个区别由消费端在 prompt 里说明。
        """
        values: list[float] = []
        for item_id in purchased:
            raw = amounts.get(item_id)
            if raw is None:
                continue
            try:
                values.append(float(raw))
            except (TypeError, ValueError):
                continue
        if not values:
            return 0.0
        return round(sum(values) / len(values), 2)
