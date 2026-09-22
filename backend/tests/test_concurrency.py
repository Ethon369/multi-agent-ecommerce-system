"""
并发正确性测试 —— 熔断窗口是进程级共享可变状态。

    为什么需要这个文件
    ────────────────
    加它之前，整套测试里【没有任何并发用例】。而熔断器是全项目唯一
    被多个请求共享的可变状态：4 个 Agent × 2 条编排路径共用同一个
    `AgentRuntime` 实例。串行测试全绿完全不能说明并发下是对的 ——
    "先读后写"型的逻辑错误在串行下永远不会暴露。

    harness/breaker.py 的模块 docstring 里有一条明确的设计声明：

        单线程事件循环下 allow() 与 record() 之间没有 await 点，
        所以不需要锁。

    这是一个【可被证伪】的声明 —— 一旦有人在两者之间插入一个 await
    （比如给 record 加个日志上报、给 allow 加个异步配置读取），
    就会出现"放行了 N 个请求，但窗口只记了 M 个"这类丢失更新，
    以及 half_open 状态下放过多个探测。这个文件就是那条声明的守卫。

    怎么"制造并发"
    ────────────
    单线程事件循环里没有真并行，但有真【交错】。办法是在桩 Agent 的
    `_execute` 里插一次 `await asyncio.sleep(0)`：它让出控制权，
    于是多个任务的 allow / record 会真正交织在一起，
    而不是被一个接一个地串完。

    这恰好也是"如果将来有人插了 await 会怎样"的运行时形态 ——
    所以用同样的方式去测它能发现同类问题。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agents.base_agent import BaseAgent
from harness.runtime import get_runtime
from models.schemas import AgentResult


class _ScriptedAgent(BaseAgent):
    """
    按脚本成功或失败的 Agent。

    `max_attempts=1` 是有意的：不重试，这样"一次 run 记一次结果"
    的计数关系是确定的，断言才写得死。重试会让窗口里的样本数
    与调用数不再一一对应，掩盖真正的计数错误。
    """

    def __init__(self, name: str, *, ok: bool) -> None:
        super().__init__(name, timeout=5.0, max_attempts=1)
        self._ok = ok
        self.executions = 0

    async def _execute(self, **kwargs: Any) -> AgentResult:
        self.executions += 1
        # 关键：让出控制权，制造真正的交错。
        await asyncio.sleep(0)
        if not self._ok:
            raise RuntimeError("scripted failure")
        return AgentResult(agent_name=self.name, success=True)


def _open_breaker(name: str, *, threshold: int = 5) -> None:
    """把某个 agent 的熔断器打到 open 状态。"""
    runtime = get_runtime()
    for _ in range(threshold):
        runtime.record(name, False)
    assert runtime.breaker_for(name).state == "open"


# ── 1. 并发失败：跳闸只发生一次 ─────────────────────────────────


@pytest.mark.anyio
async def test_concurrent_failures_trip_the_breaker_exactly_once(
    captured: list[dict[str, Any]],
) -> None:
    """
    20 个并发失败只应产生【一次】跳闸日志。

    没有这条断言时，一个"每次 record 都检查阈值"的实现会跳闸很多次 ——
    功能上看起来没坏（状态是 open），但告警日志会被刷满，
    而且说明状态迁移的判断没有幂等性。
    """
    runtime = get_runtime()
    agent = _ScriptedAgent("conc_exactly_once", ok=False)

    results = await asyncio.gather(*(agent.run(user_id="u") for _ in range(20)))

    breaker = runtime.breaker_for("conc_exactly_once")
    assert breaker.state == "open"
    assert all(not r.success for r in results)

    tripped = [e for e in captured if e.get("event") == "agent.circuit_tripped"]
    assert len(tripped) == 1, (
        f"跳闸日志出现了 {len(tripped)} 次，应为 1 次 —— 状态迁移在并发下不幂等"
    )


# ── 2. 并发失败：窗口计数不丢 ───────────────────────────────────


@pytest.mark.anyio
async def test_concurrent_failures_are_all_recorded() -> None:
    """
    每个放行过的调用都必须被记进窗口 —— 一次都不能丢。

    这是"先读后写"型 bug 的经典观测点：如果 allow 与 record 之间
    被插入了 await，或者有人把 record 写成"先取值再赋值"，
    这里就会少记。
    """
    runtime = get_runtime()
    agent = _ScriptedAgent("conc_no_lost_update", ok=False)

    await asyncio.gather(*(agent.run(user_id="u") for _ in range(20)))

    breaker = runtime.breaker_for("conc_no_lost_update")
    assert agent.executions == 20, "20 次调用都该走到 _execute（此时熔断尚未打开）"
    assert breaker.snapshot()["window_size"] == 20
    assert breaker.failures_in_window == 20
    assert breaker.error_rate == 1.0


@pytest.mark.anyio
async def test_concurrent_mixed_results_keep_the_window_consistent() -> None:
    """
    成功与失败混在一起并发时，三个派生量必须彼此自洽：
        窗口大小 == 记录数
        失败数   == 记录里 False 的个数
        错误率   == 失败数 / 窗口大小
    只要有一个对不上，就说明有人漏记或重复记。
    """
    runtime = get_runtime()
    # 关键：两个【对象】用同一个 name —— 这正是真实情形。
    # 熔断状态是按 agent 名共享的，不是按对象共享的（harness/deps.py 的组合根
    # 保证了编排器之间共享同一份 runtime）。
    same_ok = _ScriptedAgent("conc_mixed", ok=True)
    same_bad = _ScriptedAgent("conc_mixed", ok=False)
    tasks = [same_bad.run(user_id="u") for _ in range(7)] + [
        same_ok.run(user_id="u") for _ in range(3)
    ]

    await asyncio.gather(*tasks)

    snap = runtime.breaker_for("conc_mixed").snapshot()
    assert snap["window_size"] == 10
    assert snap["failures_in_window"] == 7
    assert snap["error_rate"] == 0.7


# ── 3. 被熔断挡住时【不】记失败 ─────────────────────────────────


@pytest.mark.anyio
async def test_rejected_calls_do_not_grow_the_window() -> None:
    """
    被熔断挡住的调用不能被记成失败。

    这条规则看着不起眼，但它是"熔断器能重新闭合"的前提：
    如果每次拒绝都追加一个失败，窗口会被失败永久填满 ——
    冷却期结束后 half_open 探测成功也清不干净，等于熔断打开了就再也回不来。
    （CLAUDE.md 里把这条列为设计约束，这里补上并发版本的验证。）
    """
    runtime = get_runtime()
    _open_breaker("conc_rejected")
    before = runtime.breaker_for("conc_rejected").snapshot()

    agent = _ScriptedAgent("conc_rejected", ok=False)
    results = await asyncio.gather(*(agent.run(user_id="u") for _ in range(15)))

    after = runtime.breaker_for("conc_rejected").snapshot()
    assert all(not r.success for r in results)
    assert agent.executions == 0, "熔断打开后不该再真正执行"
    assert all("circuit_open" in (r.error or "") for r in results)
    assert after["window_size"] == before["window_size"], (
        "被拒绝的调用把窗口撑大了 —— 熔断器将再也无法闭合"
    )
    assert after["failures_in_window"] == before["failures_in_window"]


# ── 4. half_open：并发下只放行【一个】探测 ──────────────────────


@pytest.mark.anyio
async def test_half_open_admits_exactly_one_concurrent_trial() -> None:
    """
    冷却期结束后，无论多少个请求同时到达，只能有一个成为探测请求。

    这是全项目最容易被并发打穿的地方：`_trial_in_flight` 是一个
    "读-判断-写"序列，如果它和 await 混在一起，10 个并发请求会
    全部成为探测请求 —— 也就是熔断形同虚设，下游会被瞬间打满。
    """
    runtime = get_runtime()
    name = "conc_half_open"
    runtime.breaker_for(name).reset_timeout_s = 0.0  # 立刻可探测

    _open_breaker(name)

    async def attempt() -> bool:
        # 让出控制权后再进熔断门，制造"同时到达"
        await asyncio.sleep(0)
        return runtime.allow(name)

    outcomes = await asyncio.gather(*(attempt() for _ in range(10)))

    assert sum(outcomes) == 1, (
        f"放行了 {sum(outcomes)} 个探测请求，应为 1 —— 熔断在并发下会失效"
    )
    assert runtime.breaker_for(name).state == "half_open"


@pytest.mark.anyio
async def test_half_open_trial_result_closes_the_breaker() -> None:
    """探测成功要闭合，而且并发场景下也只闭合一次。"""
    runtime = get_runtime()
    name = "conc_half_open_close"
    runtime.breaker_for(name).reset_timeout_s = 0.0
    _open_breaker(name)

    agent = _ScriptedAgent(name, ok=True)

    async def attempt() -> AgentResult:
        await asyncio.sleep(0)
        return await agent.run(user_id="u")

    await asyncio.gather(*(attempt() for _ in range(10)))

    assert runtime.breaker_for(name).state == "closed"
    assert agent.executions == 1, "只该有一个请求真正打到下游"
    # 闭合时窗口会被清空（breaker._close 的行为）：恢复之后重新开始统计。
    # 所以这里应当是 0 而不是 1 —— 探测结果本身也进了窗口，但随即被清掉。
    # 把这条写出来是因为它容易被想当然成"窗口里应留着那次成功"。
    assert runtime.breaker_for(name).snapshot()["window_size"] == 0


# ── 5. 不同 agent 之间互不干扰 ──────────────────────────────────


@pytest.mark.anyio
async def test_breakers_are_isolated_per_agent_under_concurrency() -> None:
    """
    A 挂了不该影响 B。

    这不是理论问题：`graph.py` 与 `supervisor.py` 曾经各自构造一套 Agent
    实例，导致"一个端点打挂的 Agent，另一个端点完全不知情" ——
    修法就是按名字共享同一个 runtime（harness/deps.py 的组合根）。
    这里验证共享之后仍然按名字隔离。
    """
    runtime = get_runtime()
    bad = _ScriptedAgent("conc_iso_bad", ok=False)
    good = _ScriptedAgent("conc_iso_good", ok=True)

    tasks = [bad.run(user_id="u") for _ in range(8)] + [
        good.run(user_id="u") for _ in range(8)
    ]
    results = await asyncio.gather(*tasks)

    assert runtime.breaker_for("conc_iso_bad").state == "open"
    assert runtime.breaker_for("conc_iso_good").state == "closed", (
        "另一个 agent 被连坐了 —— 熔断状态没有按名字隔离"
    )
    good_snap = runtime.breaker_for("conc_iso_good").snapshot()
    assert good_snap["failures_in_window"] == 0
    assert all(r.success for r in results[8:])


# ── 6. 短路不花预算 ────────────────────────────────────────────


@pytest.mark.anyio
async def test_short_circuit_costs_almost_nothing() -> None:
    """
    熔断打开后，请求应该在【几乎零耗时】内被拒绝，而不是仍然去等下游。

    这是熔断的核心价值（对外表现就是实测里那条"第 3 次请求 0ms"）。
    并发下同样成立 —— 因为短路路径上没有 await，不可能被别的任务拖住。
    """
    runtime = get_runtime()
    _open_breaker("conc_short")
    agent = _ScriptedAgent("conc_short", ok=False)

    results = await asyncio.gather(*(agent.run(user_id="u") for _ in range(10)))

    assert max(r.latency_ms for r in results) < 50.0, (
        "短路路径上花了时间 —— 说明它没有在熔断门处直接返回"
    )
    assert all(r.confidence == 0.0 for r in results), "降级结果必须显式标记置信度"
    assert runtime is get_runtime(), "运行时必须是同一份单例"
