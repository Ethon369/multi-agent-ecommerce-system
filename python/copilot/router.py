"""
运营 Copilot 的 HTTP 入口。

    与主推荐接口的关系
    ────────────────
    POST /api/v1/recommend   —— 确定性链路，工具调用是写死的
    POST /api/v1/copilot     —— 自主决策，模型自己选工具（本文件）

    两者【共享】熔断状态、指标、账本、工具注册表，
    但【不共享】会话状态（Copilot 有对话历史，推荐没有）。
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from harness.deps import get_tool_registry

logger = structlog.get_logger()

router = APIRouter(prefix="/api/v1", tags=["copilot"])

# 会话状态存在进程内存里。
#
# ⚠️ 已知限制：多 worker 部署会失效（用户第二次提问可能落到另一个进程，
#    就读不到上一轮的历史）。单进程演示够用。
#    要修的话：把 _sessions 换成 Redis（本项目 services/feature_store.py
#    已经演示了 Redis 的用法），或者干脆让客户端每次把历史带上。
_copilot = None


async def _get_copilot():
    """
    惰性创建（因为拿工具注册表是异步的 —— 装配它要去问 MCP Server）。

    做成单例而不是每请求一个：Copilot 持有【会话历史】，
    每请求重建等于每次都失忆。
    """
    global _copilot
    if _copilot is None:
        from .agent import CopilotAgent

        _copilot = CopilotAgent(await get_tool_registry())
    return _copilot


def reset_copilot() -> None:
    """丢弃单例。测试用。"""
    global _copilot
    _copilot = None


class CopilotRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str = Field(default="default", max_length=128)
    reset: bool = False
    """是否清空该会话的历史。"""


class CopilotResponse(BaseModel):
    session_id: str
    reply: str
    stop_reason: str
    steps: int
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    latency_ms: float = 0.0


@router.post("/copilot", response_model=CopilotResponse)
async def copilot(request: CopilotRequest) -> CopilotResponse:
    """
    运营助手：由模型自主决定调用哪些工具来回答问题。

    例：
        {"message": "哪些商品快断货了？"}
        {"message": "系统现在健康吗，这个月花了多少钱？"}
    """
    registry = await get_tool_registry()
    if len(registry) == 0:
        raise HTTPException(status_code=503, detail="没有可用的工具")

    agent = await _get_copilot()
    # 注册表是进程级单例，直接换上（而不是只在构造时注入）——
    # 这样测试可以塞一个假注册表进来，不用重启进程。
    agent.registry = registry

    if request.reset:
        agent.reset(request.session_id)

    result = await agent.ask(request.message, session_id=request.session_id)

    return CopilotResponse(
        session_id=request.session_id,
        reply=result.reply,
        stop_reason=result.stop_reason,
        steps=len(result.steps),
        tool_calls=result.tool_results,
        usage=result.usage,
        latency_ms=round(result.latency_ms, 1),
    )
