"""
熔断器测试 —— 零网络、零 LLM、零事件循环。

这是整个项目里唯一可以在 CI 里完全确定性地验证的核心逻辑，
所以覆盖得比其他模块厚。

冷却期一律注入极小值（0.05s），不真的等 30 秒。
"""

from __future__ import annotations

import time

import pytest

from harness.breaker import CircuitBreaker


def make(**kw) -> CircuitBreaker:
    kw.setdefault("failure_threshold", 3)
    kw.setdefault("window", 10)
    kw.setdefault("reset_timeout_s", 0.05)
    return CircuitBreaker(**kw)


# ── 构造校验 ────────────────────────────────────────────────

def test_rejects_bad_threshold() -> None:
    with pytest.raises(ValueError):
        CircuitBreaker(failure_threshold=0)


def test_rejects_window_smaller_than_threshold() -> None:
    """window < threshold 会让熔断永远无法触发 —— 这是配置错误，要在构造时就拦。"""
    with pytest.raises(ValueError):
        CircuitBreaker(failure_threshold=10, window=5)


# ── closed 态 ───────────────────────────────────────────────

def test_starts_closed_and_allows() -> None:
    b = make()
    assert b.state == "closed"
    assert b.allow() is True
    assert b.error_rate == 0.0
    assert b.opened_s_ago is None


def test_successes_do_not_trip() -> None:
    b = make()
    for _ in range(50):
        assert b.allow() is True
        b.record(True)
    assert b.state == "closed"
    assert b.error_rate == 0.0


def test_trips_at_threshold() -> None:
    b = make(failure_threshold=3)
    for i in range(2):
        b.record(False)
        assert b.state == "closed", f"第 {i + 1} 次失败不该触发（阈值 3）"

    b.record(False)
    assert b.state == "open", "第 3 次失败必须触发熔断"


def test_opens_is_rejected_without_recording() -> None:
    b = make(failure_threshold=3)
    for _ in range(3):
        b.record(False)

    assert b.state == "open"
    for _ in range(10):
        assert b.allow() is False, "open 态必须拒绝，且不消耗任何预算"


def test_window_slides_so_failures_must_be_consecutive() -> None:
    """
    滑动窗口语义：window=3, threshold=3 意味着要【连续】3 次失败才触发。
    中间的零星成功会把旧的失败挤出窗口，从而推迟触发。
    这正是滑动窗口相对累计比率的价值。
    """
    b = make(failure_threshold=3, window=3)

    b.record(False)
    b.record(False)
    b.record(True)          # 窗口 [F, F, T] -> 2 次失败
    assert b.state == "closed"

    b.record(False)         # 窗口 [F, T, F] -> 仍是 2 次失败
    assert b.state == "closed", "非连续的失败不该触发 —— 窗口在滑动"

    b.record(False)         # 窗口 [T, F, F] -> 仍是 2 次失败
    assert b.state == "closed"

    b.record(False)         # 窗口 [F, F, F] -> 3 次失败，触发
    assert b.state == "open"


# ── error_rate 的可衰减性（原本的实现是累计的，永不回落）────

def test_error_rate_is_windowed_and_decays() -> None:
    """
    滑动窗口的失败率必须会回落。

    原实现的 BaseAgent.error_rate 是累计比率（_error_count / _call_count），
    单调不降 —— 直接拿它驱动熔断会让长跑进程一旦出错就永远回不来。
    """
    b = make(failure_threshold=5, window=10)

    for _ in range(4):
        b.record(False)
    assert b.state == "closed"
    # 注意口径：分母是【已记录的样本数】，不是窗口容量。
    # 4 次调用全失败 = 错误率 1.0（而不是 4/10=0.4）。
    assert b.error_rate == 1.0
    assert b.snapshot()["window_size"] == 4

    # 窗口只有 10 个位置，塞满成功之后旧的失败被挤出
    for _ in range(10):
        b.record(True)
    assert b.error_rate == 0.0, "滑动窗口必须会衰减"
    assert b.snapshot()["window_size"] == 10


# ── half_open 态 ────────────────────────────────────────────

def test_half_open_after_cooldown() -> None:
    b = make(failure_threshold=3, reset_timeout_s=0.05)
    for _ in range(3):
        b.record(False)
    assert b.allow() is False, "冷却期内必须拒绝"

    time.sleep(0.06)
    assert b.allow() is True, "冷却期结束后必须放行探测"
    assert b.state == "half_open"


def test_half_open_admits_exactly_one() -> None:
    """探测只能是【一个】。并发放行多个探测会让熔断形同虚设。"""
    b = make(failure_threshold=3, reset_timeout_s=0.05)
    for _ in range(3):
        b.record(False)
    time.sleep(0.06)

    assert b.allow() is True, "第一个探测放行"
    assert b.allow() is False, "探测在途时不得放行第二个"
    assert b.allow() is False


def test_half_open_success_closes() -> None:
    b = make(failure_threshold=3, reset_timeout_s=0.05)
    for _ in range(3):
        b.record(False)
    time.sleep(0.06)

    assert b.allow() is True
    b.record(True)

    assert b.state == "closed"
    assert b.error_rate == 0.0, "恢复后窗口必须清空"
    assert b.opened_s_ago is None


def test_half_open_failure_reopens_with_fresh_cooldown() -> None:
    b = make(failure_threshold=3, reset_timeout_s=0.05)
    for _ in range(3):
        b.record(False)
    time.sleep(0.06)
    assert b.allow() is True
    b.record(False)

    assert b.state == "open"
    assert b.allow() is False, "探测失败后冷却期必须【重新】计时"

    time.sleep(0.06)
    assert b.allow() is True, "重新计时结束后应再给一次机会"


def test_recovery_then_normal_operation() -> None:
    """完整生命周期：closed -> open -> half_open -> closed -> 正常放行。"""
    b = make(failure_threshold=3, reset_timeout_s=0.05)
    for _ in range(3):
        b.record(False)
    assert b.state == "open"

    time.sleep(0.06)
    b.allow()
    b.record(True)
    assert b.state == "closed"

    for _ in range(20):
        assert b.allow() is True
        b.record(True)
    assert b.state == "closed"


# ── 可观测性 ────────────────────────────────────────────────

def test_snapshot_is_serialisable() -> None:
    b = make(failure_threshold=3)
    b.record(True)
    b.record(False)
    snap = b.snapshot()

    assert snap["state"] == "closed"
    assert snap["failures_in_window"] == 1
    assert snap["window_size"] == 2
    assert snap["opened_s_ago"] is None
    import json

    json.dumps(snap)  # 必须能直接进 /api/v1/metrics 的响应


def test_open_breaker_still_reports_why_it_tripped() -> None:
    """
    回归守卫：熔断打开后，窗口必须【保留】导致跳闸的那些失败。

    踩过的坑：_open() 原先清空窗口，于是跳闸瞬间日志打出
        agent.circuit_tripped  failures_in_window=0
        agent.circuit_open     error_rate=0.0
    看起来像是"因为 0 次失败所以熔断" —— 正好把证据抹掉了，
    排查时完全无法解释为什么跳闸。
    """
    b = make(failure_threshold=3, window=10)
    for _ in range(3):
        b.record(False)

    assert b.state == "open"
    assert b.failures_in_window == 3, "跳闸原因必须仍然可查"
    assert b.error_rate == 1.0
    assert b.snapshot()["window_size"] == 3


def test_open_breaker_keeps_reason_while_rejecting() -> None:
    """open 期间被拒绝的调用不记录，所以跳闸原因不会继续被冲淡。"""
    b = make(failure_threshold=3, window=10)
    for _ in range(3):
        b.record(False)

    for _ in range(20):
        assert b.allow() is False

    assert b.failures_in_window == 3
    assert b.error_rate == 1.0


def test_half_open_failure_is_counted() -> None:
    """探测失败也要进窗口，否则重开后窗口里看不到这一次。"""
    b = make(failure_threshold=3, reset_timeout_s=0.05)
    for _ in range(3):
        b.record(False)
    time.sleep(0.06)
    assert b.allow() is True
    b.record(False)

    assert b.state == "open"
    assert b.failures_in_window == 4, "探测那一次失败必须计入窗口"


def test_reset_clears_everything() -> None:
    b = make(failure_threshold=3)
    for _ in range(3):
        b.record(False)
    assert b.state == "open"

    b.reset()
    assert b.state == "closed"
    assert b.error_rate == 0.0
    assert b.allow() is True
