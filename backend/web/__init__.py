"""
接入层装配 —— 一个函数把中间件和异常处理器按正确顺序挂上。

    为什么单独抽一个 install_http_layer()
    ──────────────────────────────────
    main.py 此前的 add_middleware 是散着写的，于是"CORS 该在最外还是最内"
    这种问题只能靠读者自己推。而这一步确实有顺序要求，写反了不会报错，
    只会让 401 响应丢掉 CORS 头（浏览器报一个含糊的 CORS error），
    或者让鉴权失败没有 request_id。

    Starlette 的规则：**后 add 的在外层**。
    所以装配顺序（由内到外）是：
        路由 → ApiKey → RateLimit → RequestId → CORS

    main.py 里只留一行 install_http_layer(app, settings)，
    顺序这件事就只在这一个文件里需要被理解。
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .errors import register_exception_handlers
from .middleware import ApiKeyMiddleware, RequestIdMiddleware
from .ratelimit import RateLimitMiddleware

logger = structlog.get_logger()

#: 允许浏览器带上的自定义请求头。显式列而不是 "*"：
#: 前者能让人一眼看出"前端会带 X-API-Key 和 X-Request-ID"。
ALLOWED_HEADERS = ["Content-Type", "X-API-Key", "X-Request-ID", "Authorization"]

#: 允许浏览器 JS 读取的响应头。
#: 必须显式声明，否则这些头在 devtools 里看得到、代码里读不到。
#: 包含限流相关三个：前端要能提示"被限流了，请等 N 秒"，
#: 而 Retry-After 恰恰是它唯一能拿到等待时长的地方。
EXPOSED_HEADERS = [
    "X-Request-ID",
    "Retry-After",
    "X-RateLimit-Limit",
    "X-RateLimit-Remaining",
]


def install_http_layer(app: FastAPI, settings: object) -> None:
    """
    装配顺序 = 由内到外。不要调整，理由见模块 docstring。
    """

    # ① 异常处理器：与中间件顺序无关，先挂上保证后面任何一步出错都有规范响应。
    register_exception_handlers(app)

    # ② 鉴权（最内层）。关闭时【完全不装】—— 不装一个"内部判断开关"的
    #    中间件，因为那会让每个请求都多一次无意义的分支判断，
    #    也让"到底鉴权没有"变成一个运行时才知道的问题。
    if settings.api_key_enabled:  # type: ignore[attr-defined]
        api_key = settings.api_key  # type: ignore[attr-defined]
        if not api_key:
            # 快速失败，而不是"启用了但 key 为空 → 谁都能过"。
            # 一个配错的鉴权开关比没有鉴权更危险：它让人以为已经受保护了。
            raise RuntimeError(
                "ECOM_API_KEY_ENABLED=true 但 ECOM_API_KEY 为空。"
                "要么填一个 key，要么把开关关掉 —— 空 key 放行等于没有鉴权。"
            )
        exempt = settings.api_key_exempt_set  # type: ignore[attr-defined]
        app.add_middleware(
            ApiKeyMiddleware, api_key=api_key, exempt_paths=exempt
        )
        logger.info("http.api_key_enabled", exempt=sorted(exempt))

    # ③ 限流。
    #
    # 位置刻意在鉴权【外层】、在请求 ID【内层】：
    #   - 在鉴权外层 → 未通过鉴权的洪水也会被限流（否则 401 洪水可以
    #     无限打，虽然每次都很便宜，但没有理由不拦）。
    #   - 在请求 ID 内层 → 429 响应带 X-Request-ID。
    #     这一点和兜底的 500 是同一类问题：如果一个中间件的响应不经过
    #     RequestIdMiddleware 的 send 包装，它的头就会丢。
    #   - 它按【凭据】分桶（没有凭据才退到客户端 IP），而凭据是从 scope 的
    #     请求头直接读的，不依赖 ApiKey 中间件先跑过。
    if settings.rate_limit_enabled:  # type: ignore[attr-defined]
        rate_exempt = settings.rate_limit_exempt_set  # type: ignore[attr-defined]
        app.add_middleware(
            RateLimitMiddleware,
            limit=settings.rate_limit_requests,  # type: ignore[attr-defined]
            window_s=settings.rate_limit_window_s,  # type: ignore[attr-defined]
            exempt_paths=rate_exempt,
        )
        logger.info(
            "http.rate_limit_enabled",
            limit=settings.rate_limit_requests,  # type: ignore[attr-defined]
            window_s=settings.rate_limit_window_s,  # type: ignore[attr-defined]
            exempt=sorted(rate_exempt),
        )

    # ④ 请求 ID。在鉴权与限流外层 —— 这样 401 与 429 都带 X-Request-ID，
    #    否则这两类失败没法和客户端发来的 id 对上。
    app.add_middleware(RequestIdMiddleware)

    # ⑤ CORS（最外层）。三个原因：
    #    1. 最外层才能给【所有】响应（含 401/429/500）加上 CORS 头。
    #       放内层的话，被拒绝的响应没有 CORS 头，浏览器只会报
    #       "CORS error"，真实原因（401/429）被盖掉了。
    #    2. 它会自己处理 OPTIONS 预检并直接返回，预检不会走到鉴权/限流层 ——
    #       避开了"浏览器预检不带自定义头"那个经典坑。
    #    3. 来源从配置读（默认是开发期端口，不是 "*"），
    #       上线收紧来源时改环境变量即可，不用动代码。
    origins = settings.cors_origins  # type: ignore[attr-defined]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=ALLOWED_HEADERS,
        expose_headers=EXPOSED_HEADERS,
    )
    # 通配符时记一条 warning：它是"忘了改配置"的典型形态，
    # 日志里留个痕迹，比上线后靠人想起来要可靠。
    if origins == ["*"]:
        logger.warning("http.cors_wildcard", detail="CORS 允许来源为 *，不要在生产使用")
