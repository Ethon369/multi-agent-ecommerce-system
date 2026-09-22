"""
工具契约 —— 一个工具需要声明哪些东西。

    为什么要有这一层
    ────────────────
    这个项目里有两类"能力"：
        1. 内置的 Python 函数（查指标、查实验、列商品目录）
        2. MCP Server 提供的工具（库存服务那 4 个）

    如果各写一遍，就会有四份重复的"超时 / 重试 / 降级 / 记账 / 记日志"代码。
    更糟的是：将来加第 3 类能力（比如接一个第三方 MCP）时，
    一定会漏掉其中某一项，而且漏了不会有任何报错。

    所以把它们统一成一个契约：**ToolSpec**。
    调用方看到的都是"一个名字 + 一段描述 + 一个 JSON Schema + 一个可调用体"，
    不需要知道它背后是本地函数还是一个会起子进程的 MCP Server。

    这一层【只服务"模型自主决定调工具"的场景】（运营 Copilot）。
    主推荐链路里的库存 Agent 继续直接调 mcp_client ——
    它是代码写死的行为，套一层注册表只是增加间接性，没有收益。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

# 工具结果的默认重试范围。
# 只重试"瞬时"故障：超时和连接问题。业务异常（比如商品 ID 不存在）
# 重试多少次都是同样的结果，只会白花时间。
DEFAULT_RETRY_ON: tuple[type[BaseException], ...] = (TimeoutError, ConnectionError)


@dataclass(frozen=True)
class ToolSpec:
    """一个可被调用的能力。"""

    name: str
    description: str
    """会原样送进模型的工具列表 —— 模型靠它决定该不该调这个工具。写得含糊模型就会乱调。"""

    input_schema: dict[str, Any]
    """JSON Schema。刻意用 MCP 的原生形状（顶层是 type/properties/required），
    因为 langchain-core 的 convert_to_openai_function 原生就认这个形状
    （已实测），所以不需要任何转换代码。"""

    handler: Callable[..., Awaitable[Any]]
    """实际执行体。统一要求是 async —— 同步函数在注册时包一层即可，
    这样注册表内部不用做同步/异步分支。"""

    timeout_s: float = 5.0
    """单次尝试的超时。注意是【每次尝试】的，不是总预算。"""

    max_attempts: int = 1
    """总尝试次数。默认 1 = 不重试。

    为什么默认不重试：一个超时 3 秒的只读工具重试两次，会在一条
    本来就 16 秒的链路上再烧 6 秒，换来的还是同一个答案。
    重试是【按工具逐个决定】的，不是全局默认开。"""

    retry_on: tuple[type[BaseException], ...] = DEFAULT_RETRY_ON
    fallback: Callable[..., Any] | None = None
    """降级函数。有它才能在失败时返回一个"次优但可用"的结果，
    而不是让调用方拿到 None 自己想办法。"""

    tags: frozenset[str] = field(default_factory=lambda: frozenset({"read_only"}))
    """标签用于筛选给不给模型用。写操作应当【不带】read_only 标签，
    默认不暴露给模型，避免它误触发副作用。"""

    source: str = "builtin"
    """来源标记，仅用于观测：`builtin` / `mcp:wms` / `mcp:recommend`。
    出问题时能一眼看出是谁慢、是谁挂了。"""


@dataclass
class ToolResult:
    """
    工具调用的结果。

        `ok` 与 `degraded` 为什么要分开
        ──────────────────────────────
        降级成功时：ok=True（调用方可以继续），degraded=True（但要知道这是次优结果）。

        例：WMS 超时了，回落到本地快照 —— 这是 ok=True + degraded=True。
        如果只有 ok 一个字段，这种"用旧数据顶上了"的情况就没法表达，
        要么谎报成功、要么谎报失败，两个都不对。

        对应到 MCP 那边就是响应里的 source="fallback"。
    """

    ok: bool
    value: Any = None
    error: str | None = None
    source: str = "builtin"
    latency_ms: float = 0.0
    attempts: int = 1
    degraded: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "value": self.value,
            "error": self.error,
            "source": self.source,
            "latency_ms": round(self.latency_ms, 1),
            "attempts": self.attempts,
            "degraded": self.degraded,
        }


def fn_to_async(fn: Callable[..., Any]) -> Callable[..., Awaitable[Any]]:
    """
    把同步函数包成 async，供注册表统一调用。

    用 inspect 而不是 asyncio.iscoroutinefunction —— 后者在 Python 3.16
    会被移除（本机 3.14 上已经报 DeprecationWarning）。
    """
    import functools
    import inspect

    if inspect.iscoroutinefunction(fn):
        return fn

    @functools.wraps(fn)
    async def wrapper(**kwargs: Any) -> Any:
        return fn(**kwargs)

    return wrapper
