"""
token / 成本账本测试。

    最重要的是那条并发测试。
    ────────────────────
    编排器用 asyncio.gather 让两个 Agent 同时跑，它们会同时往账本里记数。
    这里有三层容易写错的地方，任何一层错了都会【静默】丢 token：

      ① 账本必须在 gather 【之前】放进 contextvar
         （asyncio 任务创建时复制上下文，晚了子任务拿到 None）
      ② 放进去的必须是【可变对象】，子任务改它的属性才有用
         （ContextVar.set() 在子任务里不会传播回父任务）
      ③ 读写之间不能有 await

    这类 bug 不会报错，只会让成本数字悄悄偏小。所以要显式测。
"""

from __future__ import annotations

import asyncio

import pytest

from harness.pricing import ModelPrice, PricingTable
from harness.usage import (
    UsageAccumulator,
    current_usage,
    record_usage,
    usage_scope,
)

USAGE_ONE_CALL = {
    "input_tokens": 1000,
    "output_tokens": 500,
    "total_tokens": 1500,
    "input_token_details": {"cache_read": 0},
    "output_token_details": {"reasoning": 200},
}


# ── 价格表 ──────────────────────────────────────────────────

def test_prefix_match_beats_exact_miss() -> None:
    """
    模型名常带日期/版本后缀。精确匹配会让"明明配了价格却查不到"，
    然后静默变成 None。
    """
    t = PricingTable({"my-model": ModelPrice(1.0, 2.0)})
    assert t.lookup("my-model") is not None
    assert t.lookup("my-model-2026-08") is not None, "前缀匹配失效"


def test_longest_prefix_wins() -> None:
    t = PricingTable({
        "ds": ModelPrice(1.0, 1.0),
        "ds-flash": ModelPrice(9.0, 9.0),
    })
    assert t.lookup("ds-flash-v2").input_per_1m == 9.0, "短前缀抢走了更精确的匹配"


def test_unknown_model_returns_none_never_guesses() -> None:
    """
    查不到价格必须返回 None，而不是"给个差不多的数"。
    一个看起来精确但其实是编的成本数字，比没有数字更危险 —— 你会拿它做决策。
    """
    t = PricingTable()
    assert t.lookup("totally-unknown-model") is None
    assert t.cost("totally-unknown-model", 1000, 500) is None


def test_cache_hit_is_priced_separately() -> None:
    """
    缓存命中的输入 token 要单独计价。

    实测差价高达 50 倍（deepseek-flash 命中 $0.003 vs 未命中 $0.15 每百万），
    只用一个 input 单价会把成本算得严重偏高。
    """
    price = ModelPrice(input_per_1m=100.0, output_per_1m=0.0, cached_input_per_1m=1.0)

    uncached = price.cost(1_000_000, 0, cached_tokens=0)
    fully_cached = price.cost(1_000_000, 0, cached_tokens=1_000_000)

    assert uncached == pytest.approx(100.0)
    assert fully_cached == pytest.approx(1.0)
    assert uncached / fully_cached == pytest.approx(100.0)


def test_fingerprint_is_stable_and_detects_change() -> None:
    """
    价格表指纹用于判断"优化前 vs 优化后"的成本对比是否可信 ——
    两次用的价格表不同的话，那个对比是假的。
    """
    a = PricingTable({"m": ModelPrice(1.0, 2.0)})
    b = PricingTable({"m": ModelPrice(1.0, 2.0)})
    c = PricingTable({"m": ModelPrice(1.0, 3.0)})

    assert a.fingerprint() == b.fingerprint(), "同样的表必须给出同样的指纹"
    assert a.fingerprint() != c.fingerprint(), "价格变了指纹必须变"


# ── 账本：基本累加 ──────────────────────────────────────────

def test_records_and_aggregates() -> None:
    acc = UsageAccumulator()
    acc.record("agent_a", "m1", USAGE_ONE_CALL)
    acc.record("agent_a", "m1", USAGE_ONE_CALL)
    acc.record("agent_b", "m1", USAGE_ONE_CALL)

    assert acc.llm_calls == 3
    assert acc.input_tokens == 3000
    assert acc.output_tokens == 1500
    assert acc.reasoning_tokens == 600
    assert acc.by_agent["agent_a"].calls == 2
    assert acc.by_agent["agent_b"].calls == 1


def test_reasoning_tokens_do_not_double_count_cost() -> None:
    """
    reasoning token 是【包含在】output_tokens 里的子集。
    如果把它们再加一遍，成本会翻倍。
    """
    acc = UsageAccumulator()
    acc.record("a", "m1", USAGE_ONE_CALL)

    assert acc.output_tokens == 500, "output 应当就是 500，不应该额外加上 reasoning 的 200"
    assert acc.reasoning_tokens == 200


def test_missing_usage_metadata_is_tolerated() -> None:
    """API 没返回 usage 时不能炸。"""
    acc = UsageAccumulator()
    acc.record("a", "m1", None)
    assert acc.llm_calls == 1
    assert acc.input_tokens == 0


def test_estimated_flag_is_sticky() -> None:
    acc = UsageAccumulator()
    acc.record("a", "m1", USAGE_ONE_CALL)
    assert acc.estimated is False
    acc.record("a", "m1", USAGE_ONE_CALL, estimated=True)
    assert acc.estimated is True, "只要有一次是估算的，整体就必须标记为估算"


def test_cost_is_none_if_any_model_unknown() -> None:
    """
    只要有一个模型查不到价格，整体就返回 None。

    不要"能算的部分先算出来" —— 那会得到一个看起来精确、实际漏算的数字。
    """
    acc = UsageAccumulator()
    acc.record("a", "known-model", USAGE_ONE_CALL)
    acc.record("b", "unknown-model", USAGE_ONE_CALL)

    t = PricingTable({"known-model": ModelPrice(1.0, 1.0)})
    assert acc.cost_usd(t) is None

    # 全是已知模型时应当能算出来
    acc2 = UsageAccumulator()
    acc2.record("a", "known-model", USAGE_ONE_CALL)
    assert acc2.cost_usd(t) is not None


def test_empty_accumulator_costs_zero() -> None:
    assert UsageAccumulator().cost_usd(PricingTable()) == 0.0


# ── 账本：作用域 ────────────────────────────────────────────

def test_scope_installs_and_restores() -> None:
    assert current_usage() is None
    with usage_scope() as acc:
        assert current_usage() is acc
    assert current_usage() is None, "退出作用域后必须复位，否则跨请求串账"


def test_record_usage_is_noop_without_scope() -> None:
    """没有活跃账本时静默忽略 —— 单独跑一个 Agent（如写单测）不需要先建账本。"""
    record_usage("a", "m1", USAGE_ONE_CALL)   # 不该抛异常


# ── 并发正确性（本文件最重要的一组）─────────────────────────

@pytest.mark.anyio
async def test_gather_children_share_one_ledger() -> None:
    """
    核心：gather 的两个子任务必须记进【同一个】账本。

    这对应编排器里两处 asyncio.gather —— 一次推荐有两个 Phase 是并行的。
    如果这里错了，token 数会静默偏小。
    """
    async def worker(tokens: int) -> None:
        await asyncio.sleep(0)          # 制造交错点
        record_usage("worker", "m1", {"input_tokens": tokens, "output_tokens": 0})

    with usage_scope() as acc:
        await asyncio.gather(worker(100), worker(200))

    assert acc.input_tokens == 300, (
        f"子任务的记录丢了 —— 实际只记到 {acc.input_tokens}，应当是 300"
    )


@pytest.mark.anyio
async def test_many_concurrent_writers_do_not_lose_updates() -> None:
    """再多加一点并发压力，确认没有丢更新。"""
    async def worker() -> None:
        await asyncio.sleep(0)
        record_usage("w", "m1", {"input_tokens": 1, "output_tokens": 1})

    with usage_scope() as acc:
        await asyncio.gather(*[worker() for _ in range(50)])

    assert acc.input_tokens == 50
    assert acc.llm_calls == 50


@pytest.mark.anyio
async def test_child_cannot_set_a_new_ledger() -> None:
    """
    反面用例，固化一条容易犯的错：

    子任务里 ContextVar.set() 【不会】传播回父任务。
    所以账本必须是"contextvar 里的可变对象"，而不是"contextvar 里的值"。
    如果有人改成在子任务里 set 一个新账本，父任务什么都收不到。
    """
    from harness.usage import _current

    async def bad_worker() -> None:
        _current.set(UsageAccumulator())    # 模拟"错误写法"
        record_usage("w", "m1", {"input_tokens": 999, "output_tokens": 0})

    with usage_scope() as acc:
        await asyncio.gather(bad_worker())

    assert acc.input_tokens == 0, (
        "子任务里 set 新账本居然影响到了父任务 —— 那说明上下文没有被复制，"
        "整个并发模型的前提就不成立了"
    )


@pytest.mark.anyio
async def test_two_concurrent_requests_do_not_mix() -> None:
    """
    两个并发请求的账本必须互不干扰。

    （这就是为什么账本绝不能做成模块级全局变量 —— 那样 A 的 token
    会被静默算进 B 的响应里，只在有负载时才现形。）
    """
    async def request(n: int) -> UsageAccumulator:
        with usage_scope() as acc:
            await asyncio.sleep(0)
            record_usage("a", "m1", {"input_tokens": n, "output_tokens": 0})
            await asyncio.sleep(0)
            return acc

    acc_a, acc_b = await asyncio.gather(request(10), request(20))

    assert acc_a.input_tokens == 10, f"请求 A 的账本被污染: {acc_a.input_tokens}"
    assert acc_b.input_tokens == 20, f"请求 B 的账本被污染: {acc_b.input_tokens}"
