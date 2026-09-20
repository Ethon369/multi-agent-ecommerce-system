"""
请求级追踪上下文。

    为什么是 structlog contextvars，而不是 OpenTelemetry？
    ─────────────────────────────────────────────────────
    structlog 的【默认】processor 链第一项就是 merge_contextvars
    （见 structlog/_config.py 的 _BUILTIN_DEFAULT_PROCESSORS）。
    这意味着：只要 bind 一次 request_id，之后【所有】日志 —— 包括那些
    我们一行都没改过的、散落在各 Agent 里的 logger.info —— 都会自动带上它。

    而 OTel 要先装 api + sdk + OTLP exporter，再跑一个 Collector 或开个
    SaaS 账号，一下午之后才看到第一个 span。对一个单进程、单接口的本地服务，
    它换不来 grep request_id 已经给不了的东西。

    迁移接缝：span() 的形状刻意做成 OTel span 的样子
    （name / 始末时间 / duration_ms / attributes 字典）。将来真要换 sink，
    只改这一个文件。

    什么时候该换 OTel：跨进程/多 worker（那时关联才有意义），
    或者 p50 被单进程看不见的东西主导（排队、到远端 MCP Server 的网络）。
"""

from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

import structlog
from structlog.contextvars import bind_contextvars, bound_contextvars

logger = structlog.get_logger()

# 自己的 contextvar 只为了能【读回】request_id。
# structlog 把绑定值存在私有 contextvar 里且没有公开 getter，
# 与其去碰私有 API，不如自己存一份。
_request_id: Any = None


def _get_var():
    global _request_id
    if _request_id is None:
        from contextvars import ContextVar

        _request_id = ContextVar("harness_request_id", default=None)
    return _request_id


def new_request_id() -> str:
    """生成一个新的 request id。"""
    return str(uuid.uuid4())


def current_request_id() -> str | None:
    """读回当前上下文的 request_id；不在请求上下文里时返回 None。"""
    return _get_var().get()


def bind(**fields: Any) -> None:
    """
    把字段绑进当前上下文，之后所有日志自动携带。

    典型用法是在 agent / 工具边界绑 `agent=` / `tool=` / `attempt=`。
    注意：绑定值不会泄漏给【兄弟】任务 —— asyncio 任务在创建时复制上下文，
    所以子任务里的 bind 只影响它自己和它的下游。这正是我们要的语义。
    """
    bind_contextvars(**fields)


@contextmanager
def request_context(request_id: str | None = None, **fields: Any) -> Iterator[str]:
    """
    请求级上下文。必须在 asyncio.gather 【之前】进入，
    这样子任务才会在创建时复制到它。

        with request_context(rid, user_id=uid) as rid:
            await asyncio.gather(agent_a.run(), agent_b.run())   # 两者都带 rid

    可用作装饰器之外的一般上下文管理器；返回值是最终使用的 request_id。
    """
    rid = request_id or new_request_id()
    var = _get_var()
    token = var.set(rid)
    try:
        with bound_contextvars(request_id=rid, **fields):
            yield rid
    finally:
        var.reset(token)


@contextmanager
def scope(**fields: Any) -> Iterator[None]:
    """
    bind() 的【作用域版】：退出时自动还原。

    什么时候必须用它而不是 bind()：
    当一个函数被【直接 await】（而不是作为 asyncio.gather 的子任务）时，
    它和调用方在同一个 task 里共享上下文。此时裸 bind 会泄漏给调用方，
    污染调用方之后的日志。

    实证：BaseAgent.run() 里裸 bind(agent=...) 之后，Phase 3 的
    marketing_copy 把 agent=marketing_copy 泄漏到了 supervisor.complete 上 ——
    supervisor 的日志不该说自己是 marketing_copy。

    规则：绑定「这次调用是谁」用 scope()；绑定「整个请求是什么」用 bind()。
    """
    with bound_contextvars(**fields):
        yield


@contextmanager
def span(event: str, **fields: Any) -> Iterator[None]:
    """
    给一段工作计时并记结构化事件。

    结束记 `<event>.end`（带 duration_ms），异常时记 `<event>.failed`
    （带 duration_ms / error / error_type）后原样抛出。

    刻意捕获 BaseException 而不只是 Exception：CancelledError 从 3.8 起
    就是 BaseException，客户端断连时我们仍然希望留下一条记录。
    """
    start = time.perf_counter()
    try:
        yield
    except BaseException as exc:
        logger.warning(
            f"{event}.failed",
            duration_ms=round((time.perf_counter() - start) * 1000, 1),
            error=str(exc),
            error_type=type(exc).__name__,
            **fields,
        )
        raise
    else:
        logger.info(
            f"{event}.end",
            duration_ms=round((time.perf_counter() - start) * 1000, 1),
            **fields,
        )


def configure_logging(json: bool = False) -> None:
    """
    可选的日志配置。默认【不开】—— 现有控制台输出对演示是友好的。

    之所以要有它，是因为评测运行器和将来任何 CI 都需要机器可读的行。
    注意：开启后渲染格式会变，但不影响 contextvars 合并（那在 processor 链里）。
    """
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]
    if json:
        processors.append(structlog.processors.JSONRenderer(ensure_ascii=False))
    else:
        processors.append(structlog.dev.ConsoleRenderer())
    structlog.configure(processors=processors)
