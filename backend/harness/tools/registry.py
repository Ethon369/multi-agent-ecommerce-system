"""
工具注册表 —— 统一的分发与保障层。

    一条不能越过的线
    ────────────────
    注册表是【带韧性保障的分发表】，不是 agent 框架。

    主推荐链路里的 4 个 Agent 调用工具是【按名字写死】的：
        registry.call("batch_query_stock", product_ids=[...])
    模型不参与"调哪个工具"的决策。

    唯一由模型自主选工具的地方是运营 Copilot（见 python/copilot/）。
    写这句话是为了回答一个必然会被问到的问题："这不就是 ReAct 吗？"
    —— 不是。ReAct 是"让模型决定下一步做什么"，而这里只是把
    "调用 + 超时 + 重试 + 降级 + 记账"这套重复代码收敛到一处。

    与 BaseAgent 一致的约定
    ──────────────────────
    call() 【永不抛异常】。失败是一种【值】（ToolResult.ok=False），
    不是异常。这和 BaseAgent.run() 的既有约定一致，
    也让每个调用点从 try/except 变成一句 if not res.ok。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

from .spec import ToolResult, ToolSpec

logger = structlog.get_logger()


class ToolRegistry:
    """工具的分发表。进程级单例（见 harness/deps.py）。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    # ── 注册 ────────────────────────────────────────────────────

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"工具名重复: {spec.name}")
        self._tools[spec.name] = spec
        logger.debug("tool.registered", tool=spec.name, source=spec.source,
                     tags=sorted(spec.tags))

    def register_many(self, specs: list[ToolSpec]) -> None:
        for s in specs:
            self.register(s)

    # ── 查询 ────────────────────────────────────────────────────

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self, allowed_tags: set[str] | None = None) -> list[str]:
        """按标签筛选工具名。传 None 表示不筛选。"""
        if allowed_tags is None:
            return sorted(self._tools)
        return sorted(
            n for n, s in self._tools.items() if s.tags & allowed_tags
        )

    def __len__(self) -> int:
        return len(self._tools)

    def openai_schemas(self, allowed_tags: set[str] | None = None) -> list[dict[str, Any]]:
        """
        转成可以直接喂给 `ChatOpenAI.bind_tools(...)` 的格式。

        ⚠️ 这里【不需要】任何转换代码 —— 直接吐 MCP 的原生形状。
        langchain-core 的 convert_to_openai_function 有专门分支处理
        {"name", "description", "input_schema"}（已实测，
        langchain_core/utils/function_calling.py），bind_tools 内部会调它。

        这也正是"不需要 langchain-mcp-adapters"的技术依据：
        那个库的招牌功能就是把 MCP 工具转成 LangChain 工具，
        而这一步 langchain-core 已经内置了。
        """
        names = self.names(allowed_tags)
        return [
            {
                "name": self._tools[n].name,
                "description": self._tools[n].description,
                "input_schema": self._tools[n].input_schema,
            }
            for n in names
        ]

    # ── 调用 ────────────────────────────────────────────────────

    async def call(self, name: str, **kwargs: Any) -> ToolResult:
        """
        调用一个工具。**永不抛异常。**

        流程：未知名字检查 -> 超时 + 重试 -> 降级 -> 记录
        """
        spec = self._tools.get(name)
        if spec is None:
            # 模型会幻觉出不存在的工具名。这必须是【值】而不是 KeyError，
            # 否则一次幻觉就能把整条请求打挂。
            # 返回给模型的错误信息要能被模型理解，所以带上期望的名字。
            logger.warning("tool.unknown", tool=name,
                           available=sorted(self._tools)[:10])
            return ToolResult(
                ok=False, error=f"unknown_tool:{name}", source="registry", attempts=0
            )

        start = time.perf_counter()
        last_error: str | None = None
        attempts = 0

        for attempt in range(1, max(1, spec.max_attempts) + 1):
            attempts = attempt
            try:
                async with asyncio.timeout(spec.timeout_s):
                    value = await spec.handler(**kwargs)
                latency = (time.perf_counter() - start) * 1000
                logger.info("tool.result", tool=name, ok=True, source=spec.source,
                            attempts=attempt, latency_ms=round(latency, 1))
                return ToolResult(
                    ok=True, value=value, source=spec.source,
                    latency_ms=latency, attempts=attempt,
                )
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < spec.max_attempts and isinstance(exc, spec.retry_on):
                    logger.warning("tool.retry", tool=name, attempt=attempt,
                                   error=last_error)
                    await asyncio.sleep(0.2 * attempt)   # 简单退避，避免打爆下游
                    continue
                break

        # 走到这里说明尝试都失败了。注意：asyncio.CancelledError 是
        # BaseException，不会被上面的 except Exception 抓到 —— 这是对的，
        # 客户端断连应该向上传播，而不是被降级掩盖。
        latency = (time.perf_counter() - start) * 1000

        if spec.fallback is not None:
            try:
                value = spec.fallback(**kwargs)
                logger.warning("tool.degraded", tool=name, source=spec.source,
                               error=last_error, latency_ms=round(latency, 1))
                return ToolResult(
                    ok=True, value=value, error=last_error, source=spec.source,
                    latency_ms=latency, attempts=attempts, degraded=True,
                )
            except Exception as exc:
                logger.error("tool.fallback_failed", tool=name, error=str(exc))

        logger.error("tool.result", tool=name, ok=False, source=spec.source,
                     attempts=attempts, error=last_error,
                     latency_ms=round(latency, 1))
        return ToolResult(
            ok=False, error=last_error, source=spec.source,
            latency_ms=latency, attempts=attempts,
        )

    def snapshot(self) -> list[dict[str, Any]]:
        """给 /api/v1/metrics 用的工具清单。"""
        return [
            {
                "name": s.name,
                "source": s.source,
                "tags": sorted(s.tags),
                "timeout_s": s.timeout_s,
                "max_attempts": s.max_attempts,
                "has_fallback": s.fallback is not None,
            }
            for s in sorted(self._tools.values(), key=lambda x: x.name)
        ]
