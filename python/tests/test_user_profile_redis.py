"""
用户画像 Agent 接 Redis 特征层的行为 —— 全部离线。

守着 `_collect_behavior()` 那份 docstring 里的契约：

  * 三值 source：redis / redis_empty / fallback
  * **空数据【不】回落内置兜底值** —— 替用户编造行为不是降级，是造假
  * 商品 ID 必须被 join 成【类目】再进 prompt（模型不认识 ID）
  * 三个来源的优先级：context > redis > 内置兜底值
"""

from __future__ import annotations

from typing import Any

import pytest

from agents.product_rec_agent import MOCK_PRODUCTS
from agents.user_profile_agent import SYSTEM_PROMPT, UserProfileAgent
from harness import deps

#: 商品目录里真实存在的类目集合。用来断言 join 出来的类目一定命中得上下游。
VALID_CATEGORIES = {p.category for p in MOCK_PRODUCTS}


class _FakeLLM:
    def __init__(self, payload: str = '{"segments":["active"]}') -> None:
        self.payload = payload
        self.calls: list[Any] = []

    async def ainvoke(self, messages: Any) -> Any:
        self.calls.append(messages)
        return type("Resp", (), {"content": self.payload})()

    @property
    def last_prompt(self) -> str:
        return self.calls[-1][1].content


class _StubStore:
    """假的特征存储。`result` 直接决定 get_features 返回什么。"""

    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[str] = []

    async def get_features(self, user_id: str) -> Any:
        self.calls.append(user_id)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _agent(monkeypatch: Any, store: Any) -> UserProfileAgent:
    monkeypatch.setattr(deps, "get_feature_store", lambda: store)
    agent = UserProfileAgent()
    agent.llm = _FakeLLM()
    return agent


# ── 三值 source ─────────────────────────────────────────────────


@pytest.mark.anyio
async def test_switch_off_uses_fallback(monkeypatch) -> None:
    """开关关闭（store 为 None）—— 行为和接 Redis 之前完全一致。"""
    agent = _agent(monkeypatch, None)

    result = await agent.run(user_id="u1", context={})

    assert result.data["source"] == "fallback"
    assert result.confidence == 0.7
    # 内置兜底值还在（演示模式）
    assert "手机" in agent.llm.last_prompt


@pytest.mark.anyio
async def test_redis_data_is_used(monkeypatch) -> None:
    store = _StubStore(
        {
            "user_id": "u1",
            "recent_views": ["P001", "P003"],
            "recent_purchases": ["P007"],
            "view_count_7d": 2,
            "purchase_count_30d": 1,
            "avg_order_amount": 399.0,
            "active_hours": [20, 21],
            "days_since_last_visit": 0.5,
        }
    )
    agent = _agent(monkeypatch, store)

    result = await agent.run(user_id="u1", context={})

    assert result.data["source"] == "redis"
    assert result.confidence == 0.95
    assert store.calls == ["u1"]


@pytest.mark.anyio
async def test_new_user_gets_empty_not_mock(monkeypatch) -> None:
    """
    ⚠️ 这条是"降级"和"造假"的分界线。

    读成功但用户从没被上报过 -> 必须返回空值。
    回落成写死的 "近 7 天浏览 25 次" 就是替用户编造行为：
    一个其实已经流失的用户会被喂上"活跃"的信号，模型不可能再给出
    churn_risk。（eval 的 rec_006 正是这个场景。）
    """
    agent = _agent(monkeypatch, _StubStore({}))

    result = await agent.run(user_id="u_brand_new", context={})

    assert result.data["source"] == "redis_empty"
    prompt = agent.llm.last_prompt
    assert '"view_count_7d": 0' in prompt
    assert '"view_count_7d": 25' not in prompt, "回落到 mock 就是造假"
    assert "手机" not in prompt


@pytest.mark.anyio
async def test_read_failure_falls_back(monkeypatch) -> None:
    """读失败（store 按契约返回 None）—— 这才是真正该走 fallback 的情况。"""
    agent = _agent(monkeypatch, _StubStore(None))

    result = await agent.run(user_id="u1", context={})

    assert result.data["source"] == "fallback"


@pytest.mark.anyio
async def test_store_raising_does_not_break_the_request(monkeypatch) -> None:
    """
    存储层承诺不抛，但这是最后一道防线 ——
    一个读特征的【辅助】依赖不该把整条推荐链路拖垮。
    """
    agent = _agent(monkeypatch, _StubStore(RuntimeError("boom")))

    result = await agent.run(user_id="u1", context={})

    assert result.success is True
    assert result.data["source"] == "fallback"


# ── ID -> 类目 的 join ───────────────────────────────────────────


@pytest.mark.anyio
async def test_recent_views_are_joined_into_categories(monkeypatch) -> None:
    """
    Redis 里存的是 ID，但 prompt 里必须是【类目】——
    模型手里没有商品目录，看到 P001 不可能知道它是手机；
    而下游 `product_rec_agent` 把 preferred_categories 当类目集合用。
    """
    store = _StubStore(
        {
            "user_id": "u1",
            "recent_views": ["P001", "P003"],  # iPhone(手机) / AirPods(耳机)
            "recent_purchases": ["P007"],  # 充电器(配件)
            "view_count_7d": 2,
            "purchase_count_30d": 1,
            "avg_order_amount": 399.0,
            "active_hours": [20],
            "days_since_last_visit": 0.5,
        }
    )
    agent = _agent(monkeypatch, store)

    await agent.run(user_id="u1", context={})
    prompt = agent.llm.last_prompt

    assert '"recent_views": ["手机", "耳机"]' in prompt
    assert '"recent_purchases": ["配件"]' in prompt
    assert "P001" not in prompt, "ID 不该出现在 prompt 里"


@pytest.mark.anyio
async def test_unknown_item_ids_are_dropped(monkeypatch) -> None:
    """认不出的 ID（历史脏数据）直接丢，不猜 —— 猜错一个类目下游就拿它去加权。"""
    store = _StubStore(
        {
            "user_id": "u1",
            "recent_views": ["P001", "NOT_A_REAL_ID"],
            "recent_purchases": [],
            "view_count_7d": 2,
            "purchase_count_30d": 0,
            "avg_order_amount": 0.0,
            "active_hours": [],
            "days_since_last_visit": 1.0,
        }
    )
    agent = _agent(monkeypatch, store)

    await agent.run(user_id="u1", context={})

    assert '"recent_views": ["手机"]' in agent.llm.last_prompt


@pytest.mark.anyio
async def test_joined_categories_always_exist_in_catalog(monkeypatch) -> None:
    """
    join 出来的类目必须全部是目录里真实存在的类目 ——
    否则下游 `p.category in preferred` 永远为假，类目加权静默失效，
    而 eval 的断言一条都查不出这个。
    """
    store = _StubStore(
        {
            "user_id": "u1",
            "recent_views": [p.product_id for p in MOCK_PRODUCTS],
            "recent_purchases": [p.product_id for p in MOCK_PRODUCTS],
            "view_count_7d": 15,
            "purchase_count_30d": 15,
            "avg_order_amount": 100.0,
            "active_hours": [20],
            "days_since_last_visit": 0.1,
        }
    )
    agent = _agent(monkeypatch, store)

    await agent.run(user_id="u1", context={})
    categories = UserProfileAgent._to_categories(
        [p.product_id for p in MOCK_PRODUCTS]
    )

    assert categories, "目录非空却 join 出空列表，说明映射坏了"
    assert set(categories) <= VALID_CATEGORIES


def test_to_categories_dedupes_and_preserves_order() -> None:
    assert UserProfileAgent._to_categories(["P001", "P003", "P001"]) == [
        "手机",
        "耳机",
    ]


# ── 优先级 ──────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_context_overrides_redis(monkeypatch) -> None:
    """
    逐 key 合并：context > redis > 内置兜底值。
    context 是调用方对【本次请求】的显式声明（比如刚发生的行为），
    比累计历史更新；这也让评测用例能钉死输入。
    """
    store = _StubStore(
        {
            "user_id": "u1",
            "recent_views": ["P001"],
            "recent_purchases": [],
            "view_count_7d": 1,
            "purchase_count_30d": 0,
            "avg_order_amount": 0.0,
            "active_hours": [],
            "days_since_last_visit": 0.1,
        }
    )
    agent = _agent(monkeypatch, store)

    result = await agent.run(
        user_id="u1", context={"view_count_7d": 999, "recent_views": ["耳机"]}
    )

    assert result.data["source"] == "redis", "来源仍标 redis —— 数据混合了"
    prompt = agent.llm.last_prompt
    assert '"view_count_7d": 999' in prompt
    assert '"recent_views": ["耳机"]' in prompt


# ── prompt 契约 ─────────────────────────────────────────────────


def test_prompt_declares_field_semantics() -> None:
    """
    prompt 必须写明各字段的含义。

    这是"最危险的一步"的守门人：行为数据是无过滤 json.dumps 进 prompt 的，
    而 `preferred_categories` 会被下游当类目集合用。prompt 不说清楚
    "recent_views 是类目、view_count_7d 是去重商品数"，
    模型就只能猜 —— 猜错是静默的，eval 也查不出来。
    """
    for phrase in ["recent_views", "view_count_7d", "days_since_last_visit", "类目"]:
        assert phrase in SYSTEM_PROMPT, f"prompt 里没说明 {phrase}"
    assert "全新用户" in SYSTEM_PROMPT, "必须告诉模型空数据该怎么处理"
