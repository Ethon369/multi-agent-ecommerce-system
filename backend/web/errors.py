"""
类型化错误 + 全局异常处理器。

    这个模块解决的问题
    ──────────────────
    加它之前，这个服务没有任何 exception_handler，所以：
      1. 未捕获异常走 Starlette 默认 500 —— 客户端拿到的是
         纯文本 "Internal Server Error"，没有结构、没有 request_id，
         用户说"报错了"时你无法定位是哪一个请求。
      2. AgentResult.error 里放的是 str(exc)，会原样进响应体。
         LLM SDK 的异常消息里可能带 base_url、model 名、甚至部分请求参数 ——
         这些都属于内部实现细节，不该出现在客户端。

    错误响应的统一结构
    ──────────────────
        {"error": {"code": "...", "message": "...", "request_id": "...", "details": ...}}

    - code：机器可读、稳定不变。前端据此分支（而不是去匹配中文文案）。
    - message：面向人的中文说明，可以改，不保证稳定。
    - request_id：串联日志的句柄。客户端报错时把它贴给你，一次 grep 定案。
    - details：结构化补充信息，仅在校验失败等场景出现。

    为什么 catch-all 处理器【绝不】回显 str(exc)
    ────────────────────────────────────────
    一个异常字符串里可能包含文件路径、URL、SQL、甚至 token 片段。
    回显它等于把内部结构免费送出去。所以对外只有一句通用文案，
    完整堆栈留在日志里（带 request_id）—— 排查能力一点没少，
    只是不再泄漏给客户端。
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from harness import current_request_id

logger = structlog.get_logger()

#: 对外统一使用的错误码。加新错误时在这里加常量，
#: 不要在图省事的字符串里现写 —— 前端要按它分支，写散了就守不住。
CODE_INVALID_REQUEST = "invalid_request"
CODE_UNAUTHORIZED = "unauthorized"
CODE_RATE_LIMITED = "rate_limited"
CODE_NOT_FOUND = "not_found"
CODE_METHOD_NOT_ALLOWED = "method_not_allowed"
CODE_SERVICE_UNAVAILABLE = "service_unavailable"
CODE_INTERNAL = "internal_error"


class AppError(Exception):
    """
    业务错误基类。

    与直接 raise HTTPException 的区别：
    HTTPException 把"HTTP 状态码"和"业务语义"混在一起，调用方只拿到一个数字。
    AppError 强制带上一个稳定的 code —— 前端可以据此做分支，
    而不是去匹配 message 文案（文案一改前端就坏）。
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = CODE_INTERNAL,
        status_code: int = 500,
        details: object | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.details = details


class ServiceUnavailableError(AppError):
    """
    依赖不可用（工具没装配、外部服务探活失败等）。

    单独建一个子类而不是让调用方记 status_code=503：
    错误名本身就是文档，读代码的人不需要去查 503 是什么语义。
    """

    def __init__(self, message: str, *, details: object | None = None) -> None:
        super().__init__(
            message, code=CODE_SERVICE_UNAVAILABLE, status_code=503, details=details
        )


def _request_id_of(request: Request | None) -> str | None:
    """
    取当前请求的 id。**优先读 scope，其次读上下文变量。**

    为什么不只用 current_request_id()：兜底的 500 处理器由
    ServerErrorMiddleware 调用，而它挂在所有自定义中间件的【外层】。
    异常向上穿过 RequestIdMiddleware 的 `with request_context(...)` 时
    contextvar 已经被复位，处理器里只能读到 None ——
    500 响应体里的 request_id 会是空的，而那恰恰是最需要它的时候
    （实测踩到，被 tests/test_api.py 拦下）。

    反过来，scope["state"] 是个普通 dict，随请求对象一直存在，
    不受栈展开影响。所以它是这个位置唯一可靠的来源。
    """
    if request is not None:
        state = request.scope.get("state")
        if isinstance(state, dict):
            rid = state.get("request_id")
            if rid:
                return str(rid)
    return current_request_id()


def _error_body(
    request: Request | None,
    code: str,
    message: str,
    *,
    details: object | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {
        "error": {
            "code": code,
            "message": message,
            "request_id": _request_id_of(request),
        }
    }
    if details is not None:
        body["error"]["details"] = details  # type: ignore[index]
    return body


def register_exception_handlers(app: FastAPI) -> None:
    """把四类异常的处理器挂到 app 上。启动时调一次。"""

    @app.exception_handler(AppError)
    async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        # 自己的错误类：状态码是可信的，按它回。
        # 4xx 记 warning、5xx 记 error —— 否则"用户参数填错"会淹没真实故障。
        log = logger.warning if exc.status_code < 500 else logger.error
        log(
            "http.app_error",
            code=exc.code,
            status_code=exc.status_code,
            path=request.url.path,
            message=exc.message,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(request, exc.code, exc.message, details=exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """
        请求体/查询参数校验失败。FastAPI 默认返回 {"detail": [...]}，
        与我们的结构不一致 —— 前端要为它写一套特例分支，不值当。

        details 走 jsonable_encoder：pydantic v2 的 errors() 里 ctx 字段
        可能装着 ValueError 之类的对象，直接塞进 JSONResponse 会序列化失败。
        """
        logger.warning(
            "http.validation_error",
            path=request.url.path,
            errors=len(exc.errors()),
        )
        return JSONResponse(
            status_code=422,
            content=_error_body(
                request,
                CODE_INVALID_REQUEST,
                "请求参数不合法，请检查后重试。",
                details=jsonable_encoder(exc.errors()),
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        """
        归一化框架自己抛的 HTTPException：路由不存在的 404、方法不对的 405、
        以及 copilot/router.py 里那个 503。

        不归一化的话，这些响应长着 {"detail": "Not Found"} 的样子，
        而业务错误长着 {"error": {...}} 的样子 —— 前端得写两套解析。
        """
        code = {
            401: CODE_UNAUTHORIZED,
            429: CODE_RATE_LIMITED,
            404: CODE_NOT_FOUND,
            405: CODE_METHOD_NOT_ALLOWED,
            # 503 映射成与 ServiceUnavailableError 同一个 code：
            # copilot/router.py 在"没有可用工具"时抛的就是 HTTPException(503)，
            # 而前端应当按【一个】语义分支处理"依赖不可用"，
            # 不该因为后端用了两种抛法就写两套判断。
            503: CODE_SERVICE_UNAVAILABLE,
        }.get(exc.status_code, f"http_{exc.status_code}")

        logger.warning(
            "http.http_exception",
            code=code,
            status_code=exc.status_code,
            path=request.url.path,
        )
        # detail 可能是 str 也可能是 list/dict；原样带出。
        # 这里不外泄风险 —— 这些 detail 是【我们自己】写的，不是异常字符串。
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(request, code, str(exc.detail)),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        """
        兜底。到这里说明是个没预料到的异常。

        exc_info=True 让日志里带上完整堆栈 —— 这是排查的唯一入口，
        所以它必须比响应体详细得多。响应体只有一句通用文案。
        """
        # request_id 走 _request_id_of：这个处理器由 ServerErrorMiddleware 调用，
        # 位置在所有自定义中间件的【外层】—— 异常穿过来时 contextvar 已经复位，
        # 只有 scope 上那份还在。见 _request_id_of 的注释。
        request_id = _request_id_of(request)
        logger.error(
            "http.unhandled_exception",
            path=request.url.path,
            method=request.method,
            error_type=type(exc).__name__,
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content=_error_body(
                request,
                CODE_INTERNAL,
                "服务内部错误，请稍后重试。"
                f"（请求 ID：{request_id or '无'}）",
            ),
            # ⚠️ 只有这一个处理器需要自己写 X-Request-ID，其它三个不用。
            #
            # 原因：其它处理器由 ExceptionMiddleware 调用，那一层在
            # RequestIdMiddleware 的【内层】，所以它们生成的响应会经过
            # 我们的 send 包装、头是自动加上的。
            # 而这个 500 处理器由 ServerErrorMiddleware 调用 ——
            # 它是 Starlette 固定的最外层，用户中间件包不住它，
            # 于是响应绕过了我们的包装。实测就是这样发现头丢了的。
            headers={"X-Request-ID": request_id} if request_id else None,
        )
