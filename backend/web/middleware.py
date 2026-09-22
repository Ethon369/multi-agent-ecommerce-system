"""
ASGI 中间件：请求 ID 贯穿 + API Key 鉴权。
（限流在隔壁 web/ratelimit.py，同一个写法与顺序约定。）

    两者为什么写在【纯 ASGI】而不是 BaseHTTPMiddleware
    ────────────────────────────────────────────────
    Starlette 的 BaseHTTPMiddleware 会把下游应用跑在一个【新 task】里
    （为了做流式响应的包装）。task 在创建时复制上下文 —— 所以
    "在 call_next 之前 set 的 contextvar"确实能传下去，但它多了一层
    任务切换，而且历史上这块在 contextvars 上有过若干已知坑。
    这些中间件都不需要包装响应体，纯 ASGI 写法更短也更直白：
    直接 set 上下文、直接 await self.app(...)，没有 task 切换。

    挂载顺序（在 web/__init__.py 的 install_http_layer 里体现）
    ────────────────────────────────────────────────────────
        CORS（最外） → RequestId → RateLimit → ApiKey → 路由

    - CORS 必须在最外层：这样 401 / 429 之类的错误响应也带上 CORS 头，
      否则浏览器只会报一个含糊的 "CORS error"，看不到真实原因。
    - RequestId 在 RateLimit 与 ApiKey 外层：401、429 也带 X-Request-ID，
      否则这两类失败没法和客户端发来的 id 对上。
      （⚠️ 有一个反例值得记住：web/errors.py 里兜底的 500 处理器由
       ServerErrorMiddleware 调用、位于【所有】自定义中间件的外层，
       拿不到这个包装，只能自己补头。实测踩到过。）
    - 另一个好处：CORS 中间件会【自己】处理 OPTIONS 预检并直接返回，
      预检根本不会走到限流与鉴权那层 —— 浏览器发预检时不带自定义头的
      那个经典坑，在这里天然不会踩到。
"""

from __future__ import annotations

import secrets

import structlog
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from harness import new_request_id, request_context

from .errors import CODE_UNAUTHORIZED

logger = structlog.get_logger()

#: 允许客户端自带的 request id 最长多少字符。
#: 不设上限的话，一个恶意的超长头会被原样写进每一条日志和响应头里。
MAX_INCOMING_REQUEST_ID = 64

REQUEST_ID_HEADER = b"x-request-id"


def _incoming_request_id(scope: Scope) -> str | None:
    """
    复用上游传进来的 X-Request-ID（如果有）。

    为什么要复用而不是一律新建：请求经过网关/前端代理时，上游可能已经
    生成了一个 id。此时我们再造一个，就会出现"两套 id、各查一半日志"。
    只在【没有】的时候才生成。

    只接受 ASCII 可打印字符且长度受限 —— 这个值会进日志字段和响应头，
    不校验等于把日志格式的控制权交给了调用方。
    """
    for key, value in scope.get("headers") or []:
        if key.lower() != REQUEST_ID_HEADER:
            continue
        try:
            candidate = value.decode("ascii").strip()
        except UnicodeDecodeError:
            return None
        if not candidate or len(candidate) > MAX_INCOMING_REQUEST_ID:
            return None
        if not candidate.isprintable():
            return None
        return candidate
    return None


class RequestIdMiddleware:
    """
    给每个请求绑定 request_id，并回写 X-Request-ID 响应头。

        为什么必须回写响应头
        ──────────────────
        request_id 本来已经在响应体的 recommendation.response 里了，
        但那只覆盖了推荐接口。而"用户报错"往往发生在别的接口上：
        401、422、500 —— 这些响应体里没有 request_id，用户也就无法
        把一次失败和日志里的一行对上。回了头，前端只要把 X-Request-ID
        展示出来（或在报错弹窗里带上），一次 grep 就能定案。

        注意跨域时前端读不到这个头
        ────────────────────────
        浏览器不允许 JS 读取未在 Access-Control-Expose-Headers 中声明的
        响应头。所以 main.py 的 CORS 配置里必须带 expose_headers=["X-Request-ID"]，
        否则这个头在 devtools 的网络面板里看得到、代码里却读不到 ——
        这种"看着有却拿不到"的问题最耗时间。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # websocket / lifespan 不走这条路。lifespan 尤其重要 ——
            # 它没有 request 的概念，硬套上下文会污染启动阶段的日志。
            await self.app(scope, receive, send)
            return

        request_id = _incoming_request_id(scope) or new_request_id()

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                # 去掉同名头再 append：上游中间件可能已经写过一次，
                # 响应里出现两个 X-Request-ID 会让客户端读到不确定的那个。
                headers = [
                    (k, v) for (k, v) in headers if k.lower() != REQUEST_ID_HEADER
                ]
                headers.append((REQUEST_ID_HEADER, request_id.encode("ascii")))
                message["headers"] = headers
            await send(message)

        # request_context 除了设 contextvar，还会 bind structlog 的
        # request_id 字段 —— 于是这个请求里【所有】日志（包括我们一行都
        # 没改过的那些散落在 Agent 里的 logger.info）自动带上它。
        #
        # 同时往 scope["state"] 放一份 —— 这一份不是冗余，是必需的：
        # 兜底的 500 处理器由 ServerErrorMiddleware 调用，而它在我们
        # 【外层】。异常向上穿过这里的 `with request_context(...)` 时，
        # contextvar 已经被复位了，处理器里读 current_request_id() 只会拿到
        # None（实测踩到：500 响应体里 request_id 是空的）。
        # scope 是个普通 dict，不随栈展开而失效，所以它是那个位置唯一
        # 可靠的来源。
        state = scope.get("state")
        if not isinstance(state, dict):
            # 理论上 ASGI 规定 state 是 dict，但真拿到非 dict 时
            # 不该让整个请求崩掉 —— 重建一个即可。
            state = {}
            scope["state"] = state
        state["request_id"] = request_id
        with request_context(request_id):
            await self.app(scope, receive, send_with_header)


class ApiKeyMiddleware:
    """
    API Key 鉴权。默认关闭（settings.api_key_enabled）。

        为什么默认关闭
        ────────────
        和 MCP / Redis 特征层同一个口径：新增的准入控制默认不启用，
        保证现有链路零破坏。这里尤其重要 —— eval/runner.py 是走
        HTTP 调服务做评测的，如果默认开启鉴权，12 条用例会集体 401，
        而症状看起来像"评测挂了"。

        为什么用 secrets.compare_digest
        ────────────────────────────
        普通的 == 在第一个不同字符处就返回，比较耗时随匹配前缀长度变化。
        理论上可以用它逐字节猜出 key。对本地演示服务这属于过度谨慎，
        但这行代码的成本是零，而"我们做过时序安全比较"是能说清楚的。
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        api_key: str,
        exempt_paths: set[str],
    ) -> None:
        self.app = app
        self.api_key = api_key.encode("utf-8")
        self.exempt_paths = exempt_paths

    def _is_exempt(self, path: str) -> bool:
        # 归一化尾斜杠：/health 和 /health/ 应当同等对待，
        # 否则少写一个斜杠就绕过了免鉴权名单…… 反过来也一样，
        # 写成 "/health/" 会让探活被挡。两边都归一最省事。
        return (path.rstrip("/") or "/") in self.exempt_paths

    def _provided_key(self, scope: Scope) -> str | None:
        """
        从 X-API-Key 或 Authorization: Bearer 取凭据。

        支持两种是因为两种都常见：自定义头最直白，
        Authorization 头则能直接复用现成的客户端库。
        """
        auth_header = ""
        for key, value in scope.get("headers") or []:
            lowered = key.lower()
            if lowered == b"x-api-key":
                return value.decode("latin-1").strip()
            if lowered == b"authorization":
                auth_header = value.decode("latin-1").strip()
        if auth_header.lower().startswith("bearer "):
            return auth_header[7:].strip()
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 预检不带自定义头，必然验不过。正常路径下 CORS 中间件已经
        # 在最外层拦掉了 OPTIONS，这里只是不依赖那个前提。
        if scope.get("method") == "OPTIONS" or self._is_exempt(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        provided = self._provided_key(scope)
        if provided is not None and secrets.compare_digest(
            provided.encode("utf-8"), self.api_key
        ):
            await self.app(scope, receive, send)
            return

        logger.warning(
            "http.unauthorized",
            path=scope.get("path"),
            method=scope.get("method"),
            # 只记【有没有带】凭据，绝不记凭据本身。
            had_credential=provided is not None,
        )
        # 不区分"没带"和"带错了"—— 区分开来等于告诉对方"这个 key 前缀对了"。
        response = JSONResponse(
            status_code=401,
            content={
                "error": {
                    "code": CODE_UNAUTHORIZED,
                    "message": "缺少或无效的 API Key。请在 X-API-Key 请求头中提供。",
                    "request_id": _scope_request_id(scope),
                }
            },
            headers={"WWW-Authenticate": 'ApiKey realm="multi-agent-ecommerce"'},
        )
        await response(scope, receive, send)


def _scope_request_id(scope: Scope) -> str | None:
    """读回 RequestIdMiddleware 放在 scope["state"] 里的 id。"""
    return (scope.get("state") or {}).get("request_id")
