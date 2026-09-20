"""
回归测试：重排必须复用 Phase 1 召回、且库存已检查过的候选集。

    这个 bug 长什么样
    ────────────────
    请求 num_items=5，稳定只返回 2-3 个商品，而且【没有任何报错或告警】。

    根因是流水线里两处召回集合不一致：
        Phase 1 召回(profile=None) -> MOCK_PRODUCTS[:10]（P001-P010）
        库存 Agent 检查的就是这 10 个
        Phase 2 又【重新召回一次】(带 profile) -> 全部 15 个
        重排从 15 个里挑 5 个 -> 挑中的 P011-P015 不在检查过的集合里
        -> supervisor.py 的过滤把它们刷掉
        -> "if not final_products" 的兜底【不触发】（还剩 2-3 个）
        -> 静默少于 num_items

    原理：召回 → 检查 → 排序 → 过滤，这四步必须作用在【同一个集合】上。
    这个测试就是为了保证这件事不会退化。
"""

from __future__ import annotations

from typing import Any

import pytest

from agents.product_rec_agent import MOCK_PRODUCTS, ProductRecAgent
from models.schemas import AgentResult, Product, ProductRecResult, UserProfile


@pytest.fixture
def agent() -> ProductRecAgent:
    return ProductRecAgent()


@pytest.mark.anyio
async def test_provided_candidates_skip_recall(agent, monkeypatch) -> None:
    """传了 candidates 就不该再去召回 —— 那正是集合不一致的来源。"""
    recalled: list[int] = []

    async def spy_recall(profile, limit):
        recalled.append(limit)
        return list(MOCK_PRODUCTS)

    monkeypatch.setattr(agent, "_recall", spy_recall)

    provided = list(MOCK_PRODUCTS[:3])
    result = await agent.run(user_profile=None, num_items=3, candidates=provided)

    assert recalled == [], "传了 candidates 还去召回，就会重新引入集合不一致"
    assert [p.product_id for p in result.products] == ["P001", "P002", "P003"]


@pytest.mark.anyio
async def test_without_candidates_still_recalls(agent, monkeypatch) -> None:
    """没传就自己召回 —— 单独调用本 Agent 时的原行为不能变。"""
    recalled: list[int] = []

    async def spy_recall(profile, limit):
        recalled.append(limit)
        return list(MOCK_PRODUCTS[:limit])

    monkeypatch.setattr(agent, "_recall", spy_recall)

    result = await agent.run(user_profile=None, num_items=4)

    assert recalled == [12], f"应当自己召回 num_items*3=12，实际 {recalled}"
    assert len(result.products) == 4


@pytest.mark.anyio
async def test_empty_candidates_falls_back_to_recall(agent, monkeypatch) -> None:
    """
    Phase 1 失败时 raw_products 可能是 []。
    空集合不能被当成"有效候选集"，否则重排会无米下锅、返回 0 个商品。
    """
    recalled: list[int] = []

    async def spy_recall(profile, limit):
        recalled.append(limit)
        return list(MOCK_PRODUCTS[:limit])

    monkeypatch.setattr(agent, "_recall", spy_recall)

    result = await agent.run(user_profile=None, num_items=3, candidates=[])

    assert recalled, "空 candidates 必须回落到自己召回"
    assert len(result.products) == 3


@pytest.mark.anyio
async def test_reranked_products_are_subset_of_candidates(agent, monkeypatch) -> None:
    """
    核心不变式：重排结果必须是候选集的子集。

    只要这条成立，"重排挑中的商品被按别的集合的检查结果过滤掉"就不可能发生。
    """
    provided = list(MOCK_PRODUCTS[:6])
    provided_ids = {p.product_id for p in provided}

    # 让重排故意返回一个【不在候选集里】的 ID，模拟模型幻觉
    async def fake_rerank(profile, candidates, num_items):
        return ["P001", "P999", "P002", "P003", "P004", "P005", "P006"]

    monkeypatch.setattr(agent, "_rerank", fake_rerank)

    result = await agent.run(user_profile=None, num_items=5, candidates=provided)

    got = {p.product_id for p in result.products}
    assert got <= provided_ids, f"返回了不在候选集里的商品: {got - provided_ids}"
    assert "P999" not in got, "幻觉出来的 ID 必须被丢弃"
    assert len(result.products) == 5, "候选集够 5 个，就该返回 5 个"


# ── 编排器层：确认它真的把候选集传下去了 ────────────────────

class RecordingProductRec:
    """
    记录每次调用收到了什么，并【真实复现原 bug 的条件】。

    关键：没收到 candidates 时（也就是"自己召回"），第一次和第二次返回
    【不同的集合】。这正是原 bug 的来源 —— Phase 1 和 Phase 2 各自召回，
    集合不一致，交集就少了。

    刻意造成【部分重叠】而不是完全不相交：
    完全不相交的话，supervisor 里 "if not final_products" 的兜底会触发，
    反而掩盖掉问题。必须是"剩几个但不是全部"这种最隐蔽的情况。
    """

    def __init__(self):
        self.calls: list[dict[str, Any]] = []
        self._recall_count = 0

    async def run(self, **kwargs: Any) -> ProductRecResult:
        self.calls.append(kwargs)
        n = kwargs.get("num_items", 10)
        candidates = kwargs.get("candidates")

        if candidates is None:
            self._recall_count += 1
            if self._recall_count == 1:
                # Phase 1：前 10 个（P001-P010）。库存 Agent 检查的就是这一批。
                candidates = list(MOCK_PRODUCTS[:10])
            else:
                # Phase 2 自己召回：换一批，只有 2 个和上面重叠。
                candidates = list(MOCK_PRODUCTS[8:]) + list(MOCK_PRODUCTS[:8])

        return ProductRecResult(
            agent_name="product_rec",
            success=True,
            products=list(candidates)[:n],
            data={"candidate_count": len(candidates)},
        )


class StubProfile:
    async def run(self, **kwargs: Any) -> AgentResult:
        from models.schemas import UserProfileResult

        return UserProfileResult(agent_name="user_profile", success=True, profile=UserProfile(user_id="u_x"))


class StubInventory:
    """放行全部商品 —— 这样"少返回"就只可能是重排集合不一致导致的。"""

    def __init__(self, allowed: set[str]):
        self.allowed = allowed

    async def run(self, **kwargs: Any) -> AgentResult:
        from models.schemas import InventoryResult

        return InventoryResult(
            agent_name="inventory",
            success=True,
            available_products=[p.product_id for p in kwargs.get("products", [])],
            data={"source": "fallback"},
        )


class StubCopy:
    async def run(self, **kwargs: Any) -> AgentResult:
        from models.schemas import MarketingCopyResult

        return MarketingCopyResult(agent_name="marketing_copy", success=True, copies=[])


@pytest.mark.anyio
async def test_supervisor_returns_full_num_items(monkeypatch) -> None:
    """
    端到端不变式：库存充足时，请求 N 个就必须返回 N 个。

    这是那个 bug 最直接的复现条件 —— 修复前这里会稳定返回 2-3 个（而不是 5 个），
    因为重排挑中的商品不在库存检查过的集合里、被静默过滤掉。
    """
    from models.schemas import RecommendationRequest
    from orchestrator.supervisor import SupervisorOrchestrator

    rec = RecordingProductRec()
    orch = SupervisorOrchestrator()
    monkeypatch.setattr(orch, "product_rec_agent", rec)
    monkeypatch.setattr(orch, "user_profile_agent", StubProfile())
    monkeypatch.setattr(orch, "inventory_agent", StubInventory(set()))
    monkeypatch.setattr(orch, "marketing_copy_agent", StubCopy())

    response = await orch.recommend(RecommendationRequest(user_id="u_x", num_items=5))

    assert len(rec.calls) == 2, "Phase 1 和 Phase 2 各调一次"

    phase1, phase2 = rec.calls
    assert phase1.get("candidates") is None, "Phase 1 是首次召回，没有候选集可传"

    passed = phase2.get("candidates")
    assert passed is not None, "Phase 2 没收到 candidates —— 静默丢商品的 bug 会复现"
    assert len(passed) == phase1["num_items"], (
        f"Phase 2 应当收到 Phase 1 召回的那 {phase1['num_items']} 个，实际 {len(passed)} 个"
    )
    assert len(response.products) == 5, (
        f"库存充足却只返回了 {len(response.products)} 个商品（应为 5）—— bug 复现"
    )
