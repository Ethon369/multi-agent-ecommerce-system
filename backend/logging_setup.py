"""
日志配置 —— 让 structlog 真的输出 JSON。

    为什么需要这么个文件
    ────────────────────
    项目文档里长期写着"结构化 JSON 日志"，但代码里【从来没有】调用过
    structlog.configure()：原来的 configure_logging() 写在 harness/trace.py，
    在"删死代码"那一轮因为【生产代码 0 引用】被删掉了 —— 当时删得对
    （写了没接就是投机代码），但它同时暴露了一件事：

        删掉一个没人调用的配置函数之后，"日志是 JSON" 这个能力
        就只存在于文档里了。实测 `grep structlog.configure` 只命中
        tests/conftest.py，生产运行时走的是 structlog 默认的
        彩色 ConsoleRenderer。

    所以这里不是"恢复原来那份"，而是补上真正的调用点：
    main.py 启动时调一次 configure_logging()。

    为什么用 UTC 时间戳
    ──────────────────
    Dockerfile 是 python:3.12-slim 且没有 ENV TZ，容器里是 UTC，本机是 CST。
    如果日志时间戳跟随本地时区，同一份日志从容器和从本机看起来会差 8 小时，
    排查"这个慢请求发生在什么时候"时会先把自己绕进去。
    响应的 timestamp 字段是本地时间（沿用既有契约，不动它），
    但【日志】统一 UTC —— 日志是给机器和排查用的，不是给人读的。
"""

from __future__ import annotations

import logging

import structlog

# 我们的 logger 用 PrintLogger 直接写 stdout。不接 stdlib logging 的原因：
# 这一层只需要保证【自己的日志】是 JSON。uvicorn / Starlette 的访问日志
# 走 stdlib，要一起 JSON 化需要 ProcessorFormatter + dictConfig，
# 那会把这份配置从 20 行变成 80 行，而收益只是"访问日志也变 JSON"。
# 现在先不接，等真接了日志采集再加 —— 别为了好看先把复杂度付了。

_CONFIGURED = False


def configure_logging(json_output: bool = True, level: str = "INFO") -> None:
    """
    配置 structlog。应用启动时调一次。

    ⚠️ 调用时机有要求：必须在【任何日志真正落盘之前】调用，且要在
    `structlog.get_logger()` 返回的 logger 被首次使用之前。

    为什么：本项目所有模块都在 import 期就 `logger = structlog.get_logger()`。
    好在 get_logger() 返回的是惰性代理，import 本身不产出日志，所以
    "先 import 一圈、再在 main.py 里 configure、然后才开始服务"是安全的。
    配合下面的 cache_logger_on_first_use=True，第一次真正写日志时会锁定
    这份配置 —— 一旦在那之前有日志漏出去，那批 logger 会被固化成默认配置。

    level 单独开一个参数（而不是硬编码 INFO）是因为测试要用它：
    conftest.py 把 ECOM_LOG_LEVEL 设成 DEBUG，否则 wrapper_class 会把
    debug 级日志全部丢掉，而生产代码里有 3 处 debug（tool.registered、
    mcp.tool_skipped_write、harness/llm.py 的用量回填）。
    过滤级别是【全局的】，一旦在 main 导入时设成 INFO，
    后面所有测试都会受影响 —— 这类跨测试污染本项目已经踩过一次。

    重复调用是幂等的：只第一次生效。
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    if json_output:
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer(
            # ensure_ascii=False —— 否则中文会变成 \uXXXX。
            # 项目里绝大多数日志消息和字段值都是中文，转义后 grep 中文
            # 就什么都搜不到了，等于把"日志可读"这项能力关掉。
            ensure_ascii=False
        )
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    # 未知级别名不应该让服务起不来（日志配置比日志本身次要）。
    # 兜底到 INFO，并让这一次降级本身可见。
    numeric_level = getattr(logging, level.upper(), None)
    if not isinstance(numeric_level, int):
        numeric_level = logging.INFO

    structlog.configure(
        processors=[
            # 必须【第一个】。它把 bind 进来的 request_id / agent / tool
            # 合并进 event_dict —— 也就是"M1 零改动加 request_id"那条主张
            # 的实现位置。把它挪到后面，所有日志就都不带 request_id 了，
            # 而 tests/test_trace.py 里有一条断言专门守着这个顺序。
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            # 把 exc_info 展开成 "exception" 字符串。没有它的话
            # JSONRenderer 只能序列化出一个异常对象，日志里看不到栈。
            structlog.processors.format_exc_info,
            renderer,
        ],
        # 低于 level 的日志直接丢掉，且是按【调用时】判断的。
        # 用 make_filtering_bound_logger 而不是在 processor 链里过滤：
        # 前者在日志语句执行前就返回，连 f-string 拼装都省掉。
        #
        # 注意它是【全局】设置 —— 一旦设成 INFO，debug 日志在进程里
        # 全部消失（生产正要如此，但测试要靠 ECOM_LOG_LEVEL=DEBUG 放回来）。
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _CONFIGURED = True


def reset_logging() -> None:
    """丢弃配置状态。测试用 —— 让下一个测试能重新配置。"""
    global _CONFIGURED
    _CONFIGURED = False
    structlog.reset_defaults()
