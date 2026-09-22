"""
用户画像 Agent —— 行为数据 -> 结构化画像。

    行为数据从哪来（三个来源，逐 key 优先级 context > redis > 内置兜底值）
    ────────────────────────────────────────────────────────────────
    1. `context`      调用方对【本次请求】的显式声明，优先级最高
    2. Redis          `services/feature_store.py` 的滑动窗口特征（可选依赖）
    3. 内置兜底值      写死的演示数据，只在开关关闭时使用

    来源标记是【三值】的：redis / redis_empty / fallback。
    详见 `_collect_behavior()` 的 docstring。
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from langchain_core.messages import HumanMessage, SystemMessage

from config import get_settings
from harness import build_chat_model
from models.schemas import (
    AgentResult,
    UserProfile,
    UserProfileResult,
    UserSegment,
)

from .base_agent import BaseAgent

logger = structlog.get_logger()

SYSTEM_PROMPT = """你是一个电商用户画像分析专家。根据用户的行为数据,分析用户特征并生成画像。

行为数据各字段的含义（务必按此理解，不要自行猜测）:
  recent_views           最近浏览过的【商品类目】列表，最近优先，最多 20 个
  recent_purchases       最近购买过的【商品类目】列表，最近优先
  view_count_7d          近 7 天浏览过的【去重商品数】，不是浏览次数
  purchase_count_30d     近 30 天购买过的【去重商品数】
  avg_order_amount       已购商品的平均成交金额。0 表示【没有金额数据】，
                         不是"平均 0 元"
  active_hours           近 7 天最常活跃的小时（东八区 0-23），可能为空
  days_since_last_visit  距最近一次行为的【天数】。这个值很大（比如 45）
                         说明用户很久没来了，应当认真考虑 churn_risk

如果行为数据是空的（计数为 0、列表为空、没有 days_since_last_visit），
说明这是一个【全新用户】，应当给出 segments: ["new_user"]，
不要凭空编造偏好或价格区间。

你需要输出以下JSON格式:
{
  "segments": ["new_user"|"active"|"high_value"|"price_sensitive"|"churn_risk"],
  "preferred_categories": ["类目1", "类目2"],
  "price_range": [最低价, 最高价],
  "rfm_score": {"recency": 0-1, "frequency": 0-1, "monetary": 0-1},
  "real_time_tags": {"活跃时段": "...", "偏好风格": "..."}
}

只输出JSON,不要其他内容。"""


#: 来源标记 -> 置信度。
#:
#: 对齐 inventory_agent 的做法（走通 MCP 给 0.95、降级给 0.7）——
#: 降级只标 source 不降 confidence 的话，"降级可观测"就只做了一半。
_SOURCE_CONFIDENCE = {"redis": 0.95, "redis_empty": 0.75, "fallback": 0.7}


class UserProfileAgent(BaseAgent):
    def __init__(self):
        settings = get_settings()
        super().__init__(
            name="user_profile",
            timeout=settings.agent_timeout_user_profile,
        )
        # 走工厂而不是直接 ChatOpenAI：provider 级调优参数（目前是关推理）
        # 集中在 harness/llm.py 一处。这是「抽几个字段」的确定性任务，
        # 推理只贡献延迟不贡献质量 —— 实测量级见 harness/llm.py 顶部。
        self.llm = build_chat_model("user_profile", temperature=0.3, max_tokens=1024)

        # 实时特征存储（可选的 Redis 依赖）。开关关闭时是 None ——
        # 和 InventoryAgent 的 MCP 客户端同一个口径：关闭时零新增失败面。
        from harness.deps import get_feature_store

        self.feature_store = get_feature_store()

    async def _execute(self, **kwargs: Any) -> UserProfileResult:
        user_id: str = kwargs["user_id"]
        context: dict = kwargs.get("context", {})

        behavior_data, source = await self._collect_behavior(user_id, context)

        messages = [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=f"用户ID: {user_id}\n行为数据: {json.dumps(behavior_data, ensure_ascii=False)}"),
        ]
        response = await self.llm.ainvoke(messages)

        profile_data = self._parse_profile(user_id, response.content)

        return UserProfileResult(
            success=True,
            profile=profile_data,
            data={"raw_analysis": response.content, "source": source},
            confidence=_SOURCE_CONFIDENCE[source],
        )

    @staticmethod
    def _to_categories(item_ids: list[str]) -> list[str]:
        """
        商品 ID -> 类目，保序去重。

        join 用的目录就是 `MOCK_PRODUCTS` —— 和召回用的是同一份，
        所以产出的类目一定能在 `product_rec_agent` 里命中。

        认不出的 ID（比如 Redis 里留了历史脏数据）直接丢掉，不猜 ——
        猜错一个类目，下游就是拿它去做加权的。
        """
        from .product_rec_agent import MOCK_PRODUCTS

        catalog = {p.product_id: p.category for p in MOCK_PRODUCTS}
        out: list[str] = []
        for item_id in item_ids:
            category = catalog.get(item_id)
            if category and category not in out:
                out.append(category)
        return out

    async def _collect_behavior(
        self, user_id: str, context: dict
    ) -> tuple[dict[str, Any], str]:
        """
        取行为数据。返回 (数据, 来源标记)。

            ⚠️ 7 个 key 的精确语义
            ──────────────────────
            这份数据是【无过滤】地 json.dumps 进 prompt 的，所以改一个 key
            的语义就等于改 prompt。SYSTEM_PROMPT 里必须写着同一套定义：

                recent_views          商品【类目】列表，最近优先，最多 20 个
                recent_purchases      商品【类目】列表，最近优先
                view_count_7d         近 7 天去重商品数
                purchase_count_30d    近 30 天去重商品数
                avg_order_amount      已购商品平均成交金额；0.0 = 没有金额数据
                active_hours          近 7 天触达小时 top3（东八区）
                days_since_last_visit 距最近一次行为的天数（仅 redis 来源有）

            ⚠️ 为什么 recent_views 给的是【类目】而不是商品 ID
            ────────────────────────────────────────────────
            Redis 里存的是 ID，但模型【不可能知道 P001 是手机】—— 它手里
            没有商品目录。而下游 `product_rec_agent.py:116-121` 把
            `preferred_categories` 当类目集合用（`p.category in preferred`），
            `:133` 还把它喂给 rerank 的模型。

            所以喂 ID 下去，模型只会回一个同样无效的
            `preferred_categories: ["P001"]`，类目加权【静默失效】 ——
            而 eval 的 6 条断言没有一条检查 profile 质量，跑出来还是 10/10。

            这个 ID->类目 的 join 刻意放在【本层】而不是 feature_store：
            feature_store 保持通用（只懂窗口和计数，不认识商品），
            而商品目录本来就在 agents 这一层。

            ⚠️ 三个来源逐 key 合并，优先级 context > redis > 内置兜底值
            context 是调用方对【本次请求】的显式声明（比如刚发生的行为），
            比累计历史更新；这也让评测用例能钉死输入。
            代价要清楚：**评测用例若传了 context，就永远走不到 redis 那条路。**

            ⚠️ 来源是三值的，不是两值
                "redis"       真的读到了 Redis 数据
                "redis_empty" Redis 读成功，但这用户从没被上报过（全新用户）
                "fallback"    开关关 / 读失败 / 超时

            前两者必须分开：一个是"这人确实是新的"，一个是"我们的特征服务
            出问题了"。合并成一个值，运维上就分不清"新用户来了"和"Redis 挂了"。
            （项目里已有同类先例：inventory 用 -1 区分"不在 WMS 里"和"缺货 0"。）

            ⚠️ 空数据【不】回落内置兜底值
            替用户编造行为不是降级，是造假。把"近 7 天浏览 25 次"喂给一个
            其实已经流失的用户，模型就不可能给出 churn_risk —— 等于用假数据
            把流失用户伪装成活跃用户。eval 的 rec_006（"长时间未活跃用户"）
            会在这个点上直接翻车。
        """
        fallback: dict[str, Any] = {
            "user_id": user_id,
            "recent_views": ["手机", "耳机", "平板"],
            "recent_purchases": ["充电器"],
            "view_count_7d": 25,
            "purchase_count_30d": 3,
            "avg_order_amount": 299.0,
            "active_hours": [20, 21, 22],
        }

        source = "fallback"
        merged = dict(fallback)

        if self.feature_store is not None:
            try:
                features = await self.feature_store.get_features(user_id)
            except Exception as exc:
                # 存储层承诺不抛（见那三行契约），这里是最后一道防线 ——
                # 和 inventory_agent 对 MCP 客户端的处理同一个口径。
                # 一个读特征的【辅助】依赖，不该把整条推荐链路拖垮。
                logger.error(
                    "user_profile.feature_store_unexpected",
                    user_id=user_id, error=str(exc)[:200],
                    error_type=type(exc).__name__,
                )
                features = None

            if features is None:
                # 读失败（连接异常 / 超时）—— 保持兜底值
                source = "fallback"
            elif not features:
                # 读成功，但这个用户从来没有被上报过任何行为 —— 全新用户。
                source = "redis_empty"
                merged = {
                    "user_id": user_id,
                    "recent_views": [],
                    "recent_purchases": [],
                    "view_count_7d": 0,
                    "purchase_count_30d": 0,
                    "avg_order_amount": 0.0,
                    "active_hours": [],
                }
            else:
                source = "redis"
                merged["recent_views"] = self._to_categories(features["recent_views"])
                merged["recent_purchases"] = self._to_categories(
                    features["recent_purchases"]
                )
                merged["view_count_7d"] = features["view_count_7d"]
                merged["purchase_count_30d"] = features["purchase_count_30d"]
                merged["avg_order_amount"] = features["avg_order_amount"]
                merged["active_hours"] = features["active_hours"]
                merged["days_since_last_visit"] = features["days_since_last_visit"]

        # context 最后覆盖 —— 它是调用方对本次请求的显式声明。
        # user_id 除外：它来自 kwargs，不该被 context 里的同名键顶掉。
        for key in merged:
            if key != "user_id" and key in context:
                merged[key] = context[key]

        return merged, source

    def _parse_profile(self, user_id: str, raw: str) -> UserProfile:
        try:
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0]
            data = json.loads(cleaned)
        except (json.JSONDecodeError, IndexError):
            data = {}

        segments = []
        for s in data.get("segments", ["active"]):
            try:
                segments.append(UserSegment(s))
            except ValueError:
                continue

        price_range_raw = data.get("price_range", [0, 10000])
        price_range = (
            float(price_range_raw[0]),
            float(price_range_raw[1]) if len(price_range_raw) > 1 else 10000.0,
        )

        return UserProfile(
            user_id=user_id,
            segments=segments or [UserSegment.ACTIVE],
            preferred_categories=data.get("preferred_categories", []),
            price_range=price_range,
            rfm_score=data.get("rfm_score", {}),
            real_time_tags=data.get("real_time_tags", {}),
        )
