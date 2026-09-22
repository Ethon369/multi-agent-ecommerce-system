"""
运营 Copilot —— 多轮工具调用循环（MCP Host）。

    为什么【不】继承 BaseAgent
    ────────────────────────
    BaseAgent 的契约是"整体重试 + 返回 confidence=0 的降级结果"。对多轮
    工具调用循环来说这两条都不对：

      ① 整体重试会【重放工具调用】。如果某一轮已经改过数据（哪怕当前
         工具都是只读的，将来加了写工具就会出问题），重试会重复执行。
      ② 每一次重试都要把前面几轮的 token 再烧一遍。
      ③ "confidence=0 的降级结果"对一次聊天来说是错的形状 ——
         用户要的是一段话，不是一个降级标记。

    所以这里独立实现，只【复用 harness 的零件】：
    工具注册表、trace、账本、以及注册表内建的"每个工具各自的重试与超时"。
    注意注册表那层重试是安全的 —— 因为工具是只读的、且它是【单次调用】级别的。

    这一层才是真正的 harness 工作
    ────────────────────────────
    写一个 MCP Server 只是包一层协议壳。而 Host 要自己实现：
    循环边界、消息回灌、工具失败的处理、迭代上限、强制收尾。

    三重护栏（绝不用 while True）
    ────────────────────────────
      ① max_steps      迭代次数上限
      ② deadline       墙钟时间上限
      ③ token budget   账本上的 token 上限
    触顶之后【强制收尾】—— 最后一轮不带工具，要求模型用已有信息作答。
    用户永远能拿到一段文字，而不是一个悬空的工具调用。
"""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import structlog
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from harness import build_chat_model, new_request_id, request_context
from harness.tools.registry import ToolRegistry
from harness.usage import usage_scope

logger = structlog.get_logger()

# 一次回答里最多并行调几个工具。防止模型一口气点 20 个菜把预算吃光。
MAX_PARALLEL_CALLS = 4

# 会话历史保留多少条消息。超出后从【最旧的工具往返】开始丢，
# 但系统提示和最近一轮用户提问永远保留。
MAX_HISTORY_MESSAGES = 20

COPILOT_SYSTEM_PROMPT = """你是电商运营助手。你可以调用一组工具来获取真实数据，然后基于数据回答运营人员的问题。

工作方式：
1. 先判断需不需要查数据。需要就用工具查，不要凭空猜测数字。
2. 可以连续调用多个工具。比如先看低库存清单，再逐个查具体商品的库存。
3. 拿到足够信息后，用简洁的中文给出结论和建议。

注意：
- 所有数字都必须来自工具返回的真实数据，不要编造。
- 如果某个工具调用失败，说明原因并基于已有信息作答，不要反复重试同一个工具。
- 回答面向运营人员，用业务语言，不要暴露工具名和技术细节。
"""


@dataclass
class CopilotStep:
    """循环里的一步，用于回放和观测。"""

    step: int
    tool_names: list[str] = field(default_factory=list)
    had_error: bool = False


@dataclass
class CopilotReply:
    reply: str
    stop_reason: str
    steps: list[CopilotStep]
    tool_results: list[dict[str, Any]]
    usage: dict[str, Any]
    latency_ms: float


class CopilotAgent:
    """由模型自主决定调用哪些工具的运营助手。"""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        max_steps: int = 6,
        max_wall_s: float = 40.0,
        max_tokens: int = 30_000,
        allowed_tags: set[str] | None = None,
    ) -> None:
        self.registry = registry
        self.max_steps = max_steps
        self.max_wall_s = max_wall_s
        self.max_tokens = max_tokens
        # 默认只给【只读】工具。写操作不该让模型自由触发 ——
        # 聊天框里一句模糊的话就可能导致一次真实写操作，这个风险不该由模型承担。
        self.allowed_tags = allowed_tags if allowed_tags is not None else {"read_only"}

        # 温度调低：工具调用的可靠性对温度很敏感，0.9 会让模型
        # 时而输出工具调用、时而输出一段文字。
        self.llm = build_chat_model("copilot", temperature=0.2, max_tokens=1024)

        self._sessions: dict[str, deque[BaseMessage]] = {}

    # ── 会话 ────────────────────────────────────────────────────

    def _history(self, session_id: str) -> list[BaseMessage]:
        return list(self._sessions.get(session_id, deque()))

    def _remember(self, session_id: str, messages: list[BaseMessage]) -> None:
        """
        记住这次往返。

        裁剪策略：超长时从【最旧】开始丢。因为工具往返占消息数最多，
        丢最旧的等价于丢掉最早那几轮工具调用的细节 —— 完整结论仍在
        后续的 AIMessage 文本里，所以不会失忆，只是不再回溯原始数据。
        """
        dq = self._sessions.setdefault(session_id, deque(maxlen=MAX_HISTORY_MESSAGES))
        dq.extend(messages)

    def reset(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    # ── 主循环 ──────────────────────────────────────────────────

    async def ask(self, message: str, session_id: str = "default") -> CopilotReply:
        start = time.perf_counter()
        request_id = new_request_id()

        with request_context(request_id, copilot_session=session_id), usage_scope() as usage:
            reply = await self._run(message, session_id, start, usage)

        return reply

    async def _run(
        self,
        message: str,
        session_id: str,
        start: float,
        usage: Any,
    ) -> CopilotReply:
        schemas = self.registry.openai_schemas(self.allowed_tags)
        if not schemas:
            return CopilotReply(
                reply="当前没有可用的工具，无法查询数据。",
                stop_reason="no_tools",
                steps=[], tool_results=[],
                usage=usage.as_report(_pricing()),
                latency_ms=(time.perf_counter() - start) * 1000,
            )

        # bind_tools 只做一次 —— 放在循环外。
        # 每次迭代都重新 bind 会白白重建 RunnableBinding。
        bound = self.llm.bind_tools(schemas)

        messages: list[BaseMessage] = [
            SystemMessage(content=COPILOT_SYSTEM_PROMPT),
            *self._history(session_id),
            HumanMessage(content=message),
        ]

        deadline = time.monotonic() + self.max_wall_s
        steps: list[CopilotStep] = []
        tool_results: list[dict[str, Any]] = []
        stop_reason = "max_steps"
        reply_text = ""

        for step_no in range(1, self.max_steps + 1):
            # ── 护栏 ②③：时间与 token 预算 ──
            if time.monotonic() > deadline:
                stop_reason = "budget_time"
                break
            if usage.input_tokens + usage.output_tokens > self.max_tokens:
                stop_reason = "budget_tokens"
                break

            try:
                ai: AIMessage = await bound.ainvoke(messages)
            except Exception as exc:
                # 模型本身调不通 —— 这属于基础设施故障，不是工具问题。
                logger.error("copilot.llm_failed", step=step_no, error=str(exc)[:200])
                stop_reason = "llm_error"
                reply_text = f"调用模型失败：{type(exc).__name__}。请稍后重试。"
                break

            messages.append(ai)

            tool_calls = getattr(ai, "tool_calls", None) or []
            if not tool_calls:
                # 模型给出了最终答复 —— 正常结束
                stop_reason = "completed"
                reply_text = ai.content or ""
                break

            step = CopilotStep(step=step_no)
            for tc in tool_calls[:MAX_PARALLEL_CALLS]:
                name = tc.get("name", "")
                # args 一定是 dict：langchain 的 AIMessage.tool_calls 是
                # pydantic 模型，args 字段类型就是 dict，构造时就校验过了
                # （实测：传字符串会直接 ValidationError）。
                # 所以这里不需要"解析 JSON 字符串"的防御分支 —— 那是死代码。
                args = tc.get("args") or {}

                step.tool_names.append(name)
                logger.info("copilot.tool_call", step=step_no, tool=name)

                res = await self.registry.call(name, **args)
                if not res.ok:
                    step.had_error = True

                tool_results.append({
                    "step": step_no,
                    "tool": name,
                    "ok": res.ok,
                    "degraded": res.degraded,
                    "latency_ms": round(res.latency_ms, 1),
                    "error": res.error,
                })

                # ── 关键：工具失败【回灌给模型】而不是中断循环 ──
                # 让模型自己决定怎么办（换个工具查 / 基于已有信息作答）。
                # 这正是"工具挂了仍然能给出回答"的原因。
                payload = (
                    res.value
                    if res.ok
                    else {"error": res.error, "degraded": res.degraded,
                          "hint": "该工具本次不可用，请基于其他已有信息作答或换一个工具。"}
                )
                messages.append(
                    ToolMessage(
                        content=json.dumps(payload, ensure_ascii=False, default=str),
                        tool_call_id=tc.get("id", ""),
                    )
                )

            steps.append(step)
        else:
            stop_reason = "max_steps"

        # ── 触点收尾：强制模型用已有信息作答 ──
        if stop_reason != "completed":
            logger.info("copilot.forced_synthesis", stop_reason=stop_reason)
            reply_text = await self._synthesize(messages, reply_text)

        self._remember(session_id, [HumanMessage(content=message),
                                    AIMessage(content=reply_text)])

        latency = (time.perf_counter() - start) * 1000
        logger.info(
            "copilot.complete",
            stop_reason=stop_reason,
            steps=len(steps),
            tool_calls=len(tool_results),
            latency_ms=round(latency, 1),
        )
        return CopilotReply(
            reply=reply_text,
            stop_reason=stop_reason,
            steps=steps,
            tool_results=tool_results,
            usage=usage.as_report(_pricing()),
            latency_ms=latency,
        )

    async def _synthesize(self, messages: list[BaseMessage], fallback: str) -> str:
        """
        强制收尾：去掉工具，要求模型用已有信息作答。

        为什么需要这一步：循环触顶时，最后一条消息可能是一个悬空的工具调用
        （模型想调工具但我们已经不给它机会了）。直接返回的话用户拿不到任何回答。
        tool_choice="none" 明确告诉模型"这一轮不许调工具，只能说话"。
        """
        try:
            final = await self.llm.bind(tool_choice="none").ainvoke(
                [*messages, HumanMessage(content="请基于以上已有信息直接作答，不要再调用工具。")]
            )
            return final.content or fallback or "抱歉，我没能在限定步数内完成查询。"
        except Exception as exc:
            logger.error("copilot.synthesis_failed", error=str(exc)[:200])
            return fallback or "抱歉，我没能在限定步数内完成查询。"


def _pricing():
    from harness.deps import get_pricing

    return get_pricing()
