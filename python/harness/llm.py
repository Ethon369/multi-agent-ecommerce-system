"""
LLM 客户端工厂 —— provider 相关的调优参数集中在这一个地方。

为什么需要它（不是过度抽象）
────────────────────────────
在接 harness 之前，三个 Agent 各自 `ChatOpenAI(...)` 硬编码自己的
temperature / max_tokens。这带来两个实测问题：

1. `settings.llm_temperature` / `llm_max_tokens` 是【死配置】——
   没有任何 Agent 读它们。
2. 想加一个 provider 级调优参数（比如关掉推理），得改三个文件。

实测数据（2026-09-20，deepseek-flash @ api.deepseek.com）
──────────────────────────────────────────────────────
    rerank 任务，同一 prompt，重复 6 次：
      延迟 8299 ~ 37757 ms（4.5 倍方差）
      reasoning token 与延迟的相关系数 r = 0.997
      input_tokens 每次都精确等于 612 —— 方差与 prompt / 网络无关
      98.5% 的 output token 是 reasoning token
      max_tokens=512 未生效（实测生成到 7037 个 output token）

    关掉推理后：
      9198ms -> 981ms（-89%），reasoning token 归零
      输出 JSON 合法、长度契约精确、商品 ID 全部合法，
      且 num_items=3 时排序结果与开推理【完全一致】

结论：对"选几个 ID""抽几个字段"这类确定性任务，推理只贡献延迟。
所以默认关，创作型任务通过 llm_thinking_exempt_agents 保留。

M3 会在这里把 ChatOpenAI 换成 MeteredChatOpenAI 记账 token ——
因为所有 Agent 都从这里拿客户端，改一处就全覆盖，新加 Agent 不可能漏记。
"""

from __future__ import annotations

from typing import Any

import structlog
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatResult
from langchain_openai import ChatOpenAI

from config import get_settings
from .usage import record_usage

logger = structlog.get_logger()


class MeteredChatOpenAI(ChatOpenAI):
    """
    会记账的 ChatOpenAI。

        为什么包装模型，而不是在每个调用点记账
        ────────────────────────────────────
        三个 Agent 里各有一处 `llm.ainvoke(...)`。如果在三处都手写记账，
        将来加第 4 个 Agent 时【一定会漏】—— 而且漏了不会有任何报错，
        只是成本数字悄悄少了一块。

        包装模型之后，"记账"变成了模型自带的行为：
        只要 Agent 从 build_chat_model() 拿客户端，就自动被记账。

        实现上的一个坑
        ──────────────
        ChatOpenAI 是 pydantic 模型，不能把一个"有状态的累加器"当成字段塞进来
        （pydantic 会对字段做校验/序列化，而且它也不该跟着模型走 ——
        同一个模型实例可能被多个请求复用）。

        所以累加器放在 contextvar 里，记账时按【调用时刻】去取当前请求的账本。
        见 harness/usage.py。

        对 bind_tools 的兼容
        ───────────────────
        bind_tools() 返回的是一个 RunnableBinding，但它底层仍会走到
        本类的 _agenerate —— 所以将来 Copilot 用工具调用时，记账照样生效。
    """

    agent_name: str = "unknown"
    """哪个 Agent 在用这个客户端。由 build_chat_model 注入。"""

    def _meter(self, result: ChatResult, messages: list[BaseMessage]) -> None:
        try:
            generations = getattr(result, "generations", None) or []
            if not generations:
                return
            message = generations[0].message
            usage = getattr(message, "usage_metadata", None)

            if usage:
                record_usage(self.agent_name, self.model_name, usage)
                return

            # API 没返回 usage（部分 OpenAI 兼容网关会省略）。
            # 用 tiktoken 估算，并标记 estimated=True ——
            # 让下游知道这个数字是估的，不能当实测引用。
            input_tokens = self.get_num_tokens_from_messages(messages)
            output_tokens = self.get_num_tokens(str(message.content or ""))
            record_usage(
                self.agent_name,
                self.model_name,
                {"input_tokens": input_tokens, "output_tokens": output_tokens},
                estimated=True,
            )
        except Exception as exc:
            # 记账失败绝不能影响主流程 —— 成本统计是观测，不是业务
            logger.warning("llm.metering_failed", agent=self.agent_name, error=str(exc))

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        self._meter(result, messages)
        return result

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        result = await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)
        self._meter(result, messages)
        return result


def _thinking_exempt() -> set[str]:
    raw = get_settings().llm_thinking_exempt_agents or ""
    return {a.strip() for a in raw.split(",") if a.strip()}


def build_chat_model(
    agent_name: str,
    *,
    temperature: float,
    max_tokens: int,
    disable_thinking: bool | None = None,
) -> ChatOpenAI:
    """
    构造某个 Agent 专用的 LLM 客户端。

    agent_name 不只是标签 —— 它决定这个 Agent 要不要保留推理
    （见 settings.llm_thinking_exempt_agents）。

    disable_thinking 显式传入时优先于配置，便于测试与单次覆盖。
    """
    settings = get_settings()

    if disable_thinking is None:
        disable_thinking = settings.llm_disable_thinking
        if agent_name in _thinking_exempt():
            disable_thinking = False

    extra_body: dict[str, Any] = {}
    if disable_thinking:
        # DeepSeek 的推理开关。注意这是个 provider 私有参数，
        # 换 provider 时它会被忽略（或报错）—— 所以集中在工厂里，
        # 而不是散落在三个 Agent 中。
        extra_body["thinking"] = {"type": "disabled"}

    kwargs: dict[str, Any] = {
        "api_key": settings.llm_api_key,
        "base_url": settings.llm_base_url,
        "model": settings.llm_model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        # 记账靠这个字段认领调用来源。放在这里而不是让调用方传，
        # 是为了配合"Agent 名同时决定要不要关推理"这一件事 ——
        # 两者都需要知道"是谁在调"，那就只传一次。
        "agent_name": agent_name,
    }
    if extra_body:
        kwargs["extra_body"] = extra_body

    logger.debug(
        "llm.client_built",
        agent=agent_name,
        model=settings.llm_model,
        thinking_disabled=disable_thinking,
    )
    return MeteredChatOpenAI(**kwargs)
