"""
进程内滑动窗口限流。

    为什么需要它
    ──────────
    推荐接口单次耗时约 3 秒、成本约 $0.0005（实测数字，见 eval_results/）。
    一个失控的调用方 —— 忘加 sleep 的 for 循环、写错的压测脚本、被刷的
    演示地址 —— 能在几分钟内把额度烧掉，而且因为没有限流，服务本身
    不会给出任何信号（只会看到延迟上升）。

    为什么是滑动窗口而不是固定窗口
    ────────────────────────────
    固定窗口（"每分钟重置计数"）在窗口边界有一个经典漏洞：
    第 59 秒打满 N 次、第 61 秒再打满 N 次 —— 两秒内放过了 2N 次，
    也就是设计速率的两倍。滑动窗口没有这个问题。

    代价是每个 client 要存一串时间戳。对本项目量级（单进程、演示/内网）
    完全无所谓；真上量的做法是把计数器放到 Redis（那样多 worker 也能共享），
    但一个进程内的 demo 服务为它引入一次网络往返不划算。
    ⚠️ 已知限制：多 worker 部署时每个 worker 各算各的，实际速率是 N 倍。
       单进程演示够用；要精确就得挪到 Redis。

    并发说明
    ────────
    与 harness/breaker.py 同一个理由：`_hit()` 内部没有任何 await 点，
    单线程事件循环下就是原子的，所以【不加锁】。加一把永远不会争用的锁，
    只会让读者以为这里存在真并发。
    `tests/test_concurrency.py` 里有并发用例守着这个前提。
"""

from __future__ import annotations

import hashlib
import time
from collections import deque

import structlog
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .errors import CODE_RATE_LIMITED

logger = structlog.get_logger()

#: 每多少次请求做一次全量清扫。
#: 没有清扫的话，"每个陌生 IP 留一个空 deque"会把内存慢慢啃光 ——
#: 这是所有"按 client 建桶"的限流器最容易漏的一点。
SWEEP_EVERY = 512


class RateLimitMiddleware:
    """
    滑动窗口限流。按【凭据哈希】分桶，没有凭据时按客户端 IP。

        为什么不读 X-Forwarded-For
        ────────────────────────
        客户端可以随便伪造它。直接拿它分桶等于把"换个假 IP 就绕过限流"
        送给调用方。生产环境的正确做法是在网关/负载均衡那一层限流，
        或者在受信任代理后面显式配置可信跳数 —— 那是一个需要知道
        部署拓扑才能做的决定，不该由这个中间件猜。
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        limit: int,
        window_s: float,
        exempt_paths: set[str],
    ) -> None:
        if limit < 1:
            raise ValueError("限流阈值必须 >= 1；想要不限流请把开关关掉")
        self.app = app
        self.limit = limit
        self.window_s = window_s
        self.exempt_paths = exempt_paths
        self._buckets: dict[str, deque[float]] = {}
        self._hits = 0

    # ── 分桶 ──────────────────────────────────────────────────
    @staticmethod
    def _client_key(scope: Scope) -> str:
        headers: dict[bytes, bytes] = {
            k.lower(): v for k, v in (scope.get("headers") or [])
        }
        raw = headers.get(b"x-api-key") or b""
        if not raw:
            auth = headers.get(b"authorization", b"")
            if auth.lower().startswith(b"bearer "):
                raw = auth[7:].strip()

        if raw:
            # 存哈希而不是明文：这个值会长期留在内存里，而明文凭据
            # 一旦被某次调试打印出来就是泄漏。限流只需要"能区分不同调用方"，
            # 不需要知道是谁 —— 哈希足够，而且是单向的。
            return "key:" + hashlib.sha256(raw).hexdigest()[:16]

        client = scope.get("client")
        return f"ip:{client[0]}" if client else "ip:unknown"

    def _is_exempt(self, path: str) -> bool:
        # 与鉴权中间件同一个口径：归一化尾斜杠，避免 /health/ 把探活漏进限流。
        return (path.rstrip("/") or "/") in self.exempt_paths

    # ── 计数 ──────────────────────────────────────────────────
    def _hit(self, key: str) -> tuple[bool, float]:
        """
        记一次调用并判断是否放行。

        返回 (是否放行, 建议重试等待秒数)。
        """
        now = time.monotonic()
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = deque()
            self._buckets[key] = bucket

        # 淘汰滑出窗口的旧记录 —— 这就是"滑动"的含义。
        cutoff = now - self.window_s
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

        if len(bucket) >= self.limit:
            # 最早那次一滑出窗口就放行，所以等待时间是确定的、可解释的，
            # 不是"再等一个整窗口"。
            return False, max(self.window_s - (now - bucket[0]), 0.0)

        bucket.append(now)
        self._sweep_if_due(now)
        return True, 0.0

    def _sweep_if_due(self, now: float) -> None:
        self._hits += 1
        if self._hits % SWEEP_EVERY:
            return
        # 整桶都滑出窗口的才清 —— 只要桶里还有活跃记录就留着。
        cutoff = now - self.window_s
        for key in [k for k, b in self._buckets.items() if not b or b[-1] <= cutoff]:
            self._buckets.pop(key, None)

    # ── ASGI ──────────────────────────────────────────────────
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 预检不带自定义头、也不该被限流（它由 CORS 中间件在最外层就地返回，
        # 正常路径下根本走不到这里；这里是"不依赖那个前提"的兜底）。
        if scope.get("method") == "OPTIONS" or self._is_exempt(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        key = self._client_key(scope)
        allowed, retry_after = self._hit(key)
        if allowed:
            await self.app(scope, receive, send)
            return

        logger.warning(
            "http.rate_limited",
            path=scope.get("path"),
            method=scope.get("method"),
            limit=self.limit,
            window_s=self.window_s,
            retry_after_s=round(retry_after, 1),
            # 记桶前缀（key:/ip:）而不是完整 key —— 既能区分"是凭据还是 IP 被限"，
            # 又不会把凭据标识写进日志。
            bucket=key.split(":", 1)[0],
        )

        headers = {
            # 必须取整：Retry-After 的合法形式是整数秒或 HTTP 日期，
            # 发一个 0.42 会被部分客户端直接忽略。
            "Retry-After": str(max(int(retry_after) + 1, 1)),
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": "0",
        }
        response = JSONResponse(
            status_code=429,
            content={
                "error": {
                    "code": CODE_RATE_LIMITED,
                    "message": (
                        f"请求过于频繁（{self.limit} 次 / {self.window_s:g} 秒）。"
                        f"请 {headers['Retry-After']} 秒后重试。"
                    ),
                    "request_id": (scope.get("state") or {}).get("request_id"),
                    "details": {"retry_after_s": round(retry_after, 2)},
                }
            },
            headers=headers,
        )
        await response(scope, receive, send)
