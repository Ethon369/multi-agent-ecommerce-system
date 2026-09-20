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

    什么时候该换 OTel：跨进程 / 多 worker（那时关联才有意义），
    或者 p50 被单进程看不见的东西主导（排队、到远端 MCP Server 的网络）。
    到那时把本模块的 emit 点接到 OTel SDK 即可 —— 所有日志都经过这里，
    改动面是一个文件。

    【已删除】原先还有 span() 和 configure_logging() 两个函数。
    删掉的理由：写完发现没有任何调用点用到它们（grep 生产代码 0 引用）。
    它们属于"将来可能有用"的投机代码 —— 而项目当下既没有多进程，
    也没有第二个日志 sink。留着只会让读者多问一句"这个用了没"。
    真需要时再加。
"""

from __future__ import annotations

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
