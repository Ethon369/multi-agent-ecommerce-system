"""
熔断器 —— 纯逻辑，零依赖，可完全离线单测。

为什么单独一个文件
──────────────────
它是整个仓库里【唯一】不依赖 LLM、网络、事件循环就能验证正确性的部分。
所以它承担了最重的单元测试，也是 harness 层最硬的证据。

为什么是计数触发而不是错误率触发
────────────────────────────────
`BaseAgent.error_rate` 原本是【累计】比率，永不衰减。直接拿它驱动熔断，
长跑进程里一旦出错就永久打开，再也回不来（面试官一定会问这个）。
这里改成【滑动窗口内失败计数 >= 阈值】：
    window=20, failure_threshold=5
稳态下它与"错误率 >= 25%"等价，但在恢复期行为正确，
也不需要额外的 min_samples 保护。

并发说明
────────
单线程事件循环下 allow() 与 record() 之间没有 await 点，
所以不需要锁。这里明确写下来，而不是加一个永远不会争用的
asyncio.Lock —— 那只会误导读者以为存在真并发。
"""

from __future__ import annotations

import time
from collections import deque
from typing import Literal

BreakerState = Literal["closed", "open", "half_open"]


class CircuitBreaker:
    """
    三态熔断器。

    closed     正常放行，记录结果到滑动窗口
    open       直接拒绝（不消耗任何 LLM 预算），直到冷却期结束
    half_open  冷却期后放行【恰好一次】探测
                 成功 -> closed，清空窗口
                 失败 -> 重新 open，冷却期重新计时
    """

    def __init__(
        self,
        name: str = "default",
        failure_threshold: int = 5,
        window: int = 20,
        reset_timeout_s: float = 30.0,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold 必须 >= 1")
        if window < failure_threshold:
            raise ValueError("window 不能小于 failure_threshold，否则永远无法触发")
        self.name = name
        self.failure_threshold = failure_threshold
        self.window = window
        self.reset_timeout_s = reset_timeout_s

        self._results: deque[bool] = deque(maxlen=window)
        self._state: BreakerState = "closed"
        self._opened_at: float | None = None
        self._trial_in_flight = False

    # ── 只读视图 ──────────────────────────────────────────────
    @property
    def state(self) -> BreakerState:
        return self._state

    @property
    def error_rate(self) -> float:
        """
        失败率 = 窗口内失败数 / 【已记录的样本数】。

        分母是已记录的样本数，不是窗口容量 —— 刚启动只记录 4 次且全失败时
        错误率是 1.0，不是 4/window。这是标准的错误率口径。

        与原 BaseAgent.error_rate 的关键差别：那个是【累计】比率，
        单调不降；这个是滑动窗口，会衰减。
        """
        if not self._results:
            return 0.0
        return sum(1 for ok in self._results if not ok) / len(self._results)

    @property
    def failures_in_window(self) -> int:
        return sum(1 for ok in self._results if not ok)

    @property
    def opened_s_ago(self) -> float | None:
        if self._opened_at is None:
            return None
        return time.monotonic() - self._opened_at

    def snapshot(self) -> dict[str, object]:
        """给日志和 /api/v1/metrics 用的可序列化快照。"""
        return {
            "name": self.name,
            "state": self._state,
            "error_rate": round(self.error_rate, 4),
            "failures_in_window": self.failures_in_window,
            "window_size": len(self._results),
            "opened_s_ago": (
                round(self.opened_s_ago, 1) if self._opened_at is not None else None
            ),
        }

    # ── 控制面 ────────────────────────────────────────────────
    def allow(self) -> bool:
        """
        是否放行这次调用。

        副作用：open 且冷却期已过时，会转成 half_open 并占用那【唯一一次】探测名额。
        所以它必须和 record() 配对使用，不能只调 allow 不调 record。
        """
        if self._state == "closed":
            return True

        if self._state == "open":
            ago = self.opened_s_ago
            if ago is None or ago < self.reset_timeout_s:
                return False
            # 冷却期结束：放行一次探测
            self._state = "half_open"
            self._trial_in_flight = True
            return True

        # half_open：探测还没回来就不再放行第二个
        if self._trial_in_flight:
            return False
        self._trial_in_flight = True
        return True

    def record(self, ok: bool) -> None:
        """记录一次调用的最终结果（不是单次尝试的结果）。"""
        if self._state == "half_open":
            self._trial_in_flight = False
            # 探测结果也要进窗口，否则重开后的 failures_in_window 会漏掉这一次
            self._results.append(ok)
            if ok:
                self._close()
            else:
                self._open()
            return

        self._results.append(ok)

        if self._state == "closed" and self.failures_in_window >= self.failure_threshold:
            self._open()

    def reset(self) -> None:
        """手动复位。测试和运维用（比如运维知道下游已经修好了）。"""
        self._close()

    # ── 内部状态迁移 ──────────────────────────────────────────
    def _open(self) -> None:
        """
        打开熔断。刻意【不】清空窗口。

        踩过的坑：原先这里 clear()，导致熔断跳闸瞬间
        failures_in_window=0、error_rate=0.0 —— 日志里看起来像是
        "因为 0 次失败所以熔断"，正好把证据抹掉了。
        窗口保留下来，跳闸原因在 open 期间始终可查。
        """
        self._state = "open"
        self._opened_at = time.monotonic()

    def _close(self) -> None:
        """闭合熔断，同时清空窗口 —— 恢复后重新开始统计。"""
        self._state = "closed"
        self._opened_at = None
        self._trial_in_flight = False
        self._results.clear()
