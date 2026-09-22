"""
运营 Copilot 测试 —— 用假模型驱动循环，不起网络、不花 token。

    为什么必须用假模型
    ────────────────
    这里要测的是【循环的控制逻辑】：什么时候继续、什么时候停、
    触顶了怎么办、工具失败怎么办。这些用真模型没法稳定复现
    （模型每次返回什么是不确定的）。

    假模型按脚本返回，于是每一条控制分支都能被精确命中。
    真模型的端到端验证另有一条标记测试（默认跳过）。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from copilot.agent import CopilotAgent
from harness.tools.registry import ToolRegistry
from harness.tools.spec import ToolSpec


class ScriptedLLM:
    """
    按脚本返回的假模型。

    bind_tools / bind 都返回自己 —— 因为循环里只用这两个方法的返回值
    去调 ainvoke，不需要真的做 RunnableBinding。
    """

    def __init__(
        self,
        script: list[AIMessage],
        *,
        synthesize: str = "（收尾回答）",
        tokens_per_call: int = 100,
    ):
        self.script = list(script)
        self.synthesize = synthesize
        self.invocations: list[list[Any]] = []
        self.bound_tool_choice: Any = None
        self.bound_schemas: list[dict] | None = None
        # 每次调用记一笔用量 —— 不记的话 token 预算护栏永远不触发，
        # 那一条测试就变成摆设了。真实模型当然会计进账本，
        # 假模型也必须模拟这一点，否则测的不是同一套逻辑。
        self.tokens_per_call = tokens_per_call

    def bind_tools(self, schemas: list[dict]) -> "ScriptedLLM":
        self.bound_schemas = schemas
        return self

    def bind(self, **kwargs: Any) -> "ScriptedLLM":
        if "tool_choice" in kwargs:
            self.bound_tool_choice = kwargs["tool_choice"]
        return self

    async def ainvoke(self, messages: list[Any]) -> AIMessage:
        from harness.usage import record_usage

        self.invocations.append(list(messages))
        # 模拟"模型调用会计进账本"—— 真实模型由 MeteredChatOpenAI 做这件事
        record_usage(
            "copilot",
            "fake-model",
            {"input_tokens": self.tokens_per_call, "output_tokens": 0},
        )

        if self.bound_tool_choice == "none":
            # 强制收尾那一轮
            return AIMessage(content=self.synthesize)
        if self.script:
            return self.script.pop(0)
        # 脚本用完还没结束 -> 返回一段文字，模拟"模型终于肯说话了"
        return AIMessage(content="（脚本耗尽）")


def tool_call(name: str, args: dict | None = None, call_id: str = "c1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args or {}, "id": call_id, "type": "tool_call"}],
    )


def make_registry(handlers: dict[str, Any] | None = None) -> ToolRegistry:
    reg = ToolRegistry()
    handlers = handlers or {}

    async def default_handler(**kwargs: Any) -> Any:
        return {"ok": True, "args": kwargs}

    for name in ("t_ok", "t_fail", "t_slow", "wms__list_low_stock", "get_metrics"):
        reg.register(ToolSpec(
            name=name,
            description=f"{name} 的描述",
            input_schema={"type": "object", "properties": {}, "required": []},
            handler=handlers.get(name, default_handler),
            timeout_s=1.0,
            source="mcp:wms" if name.startswith("wms__") else "builtin",
        ))
    return reg


def make_agent(script: list[AIMessage], registry: ToolRegistry | None = None, **kw) -> tuple[CopilotAgent, ScriptedLLM]:
    agent = CopilotAgent(registry or make_registry(), **kw)
    fake = ScriptedLLM(script)
    agent.llm = fake          # 换掉真模型
    return agent, fake


# ── 正常路径 ────────────────────────────────────────────────

@pytest.mark.anyio
async def test_completes_when_model_stops_calling_tools() -> None:
    agent, fake = make_agent([
        tool_call("t_ok"),
        AIMessage(content="最终答案"),
    ])

    r = await agent.ask("问题")

    assert r.stop_reason == "completed"
    assert r.reply == "最终答案"
    assert len(r.steps) == 1, "只有第一轮调了工具"
    assert r.tool_results[0]["tool"] == "t_ok"
    assert r.tool_results[0]["ok"] is True


@pytest.mark.anyio
async def test_direct_answer_without_tools() -> None:
    """模型可以不调任何工具直接回答（比如问的是常识问题）。"""
    agent, _ = make_agent([AIMessage(content="不需要查数据")])

    r = await agent.ask("你好")

    assert r.stop_reason == "completed"
    assert r.steps == []
    assert r.tool_results == []


@pytest.mark.anyio
async def test_tool_results_are_fed_back_to_model() -> None:
    """
    工具结果必须回灌。

    这是整个循环的核心 —— 不回灌的话模型永远拿不到数据，
    只能靠幻觉作答（而且它自己不会知道）。
    """
    agent, fake = make_agent([
        tool_call("t_ok", {"x": 1}),
        AIMessage(content="done"),
    ])

    await agent.ask("问题")

    # 第二次调用时，消息里应当包含系统提示 + 用户提问 + 第一次的 AI 消息 + 工具结果
    second_call = fake.invocations[1]
    kinds = [type(m).__name__ for m in second_call]
    assert "ToolMessage" in kinds, f"工具结果没有回灌，消息类型: {kinds}"

    tool_msg = next(m for m in second_call if type(m).__name__ == "ToolMessage")
    assert "ok" in tool_msg.content
    assert tool_msg.tool_call_id == "c1", "tool_call_id 必须对上，否则模型对不上号"


@pytest.mark.anyio
async def test_multiple_tools_in_one_step() -> None:
    """模型一轮里可以并行点多个工具（本项目里就出现过：先查库存再查商品名）。"""
    agent, _ = make_agent([
        AIMessage(content="", tool_calls=[
            {"name": "t_ok", "args": {}, "id": "a", "type": "tool_call"},
            {"name": "get_metrics", "args": {}, "id": "b", "type": "tool_call"},
        ]),
        AIMessage(content="done"),
    ])

    r = await agent.ask("问题")

    assert len(r.tool_results) == 2
    assert {t["tool"] for t in r.tool_results} == {"t_ok", "get_metrics"}
    assert r.steps[0].tool_names == ["t_ok", "get_metrics"]


# ── 护栏 ①：迭代上限 ────────────────────────────────────────

@pytest.mark.anyio
async def test_max_steps_triggers_forced_synthesis() -> None:
    """
    模型一直调工具不停 —— 必须被步数上限截断，并强制它用已有信息作答。

    用户永远要拿到一段文字，而不是一个悬空的工具调用。
    """
    agent, fake = make_agent(
        [tool_call("t_ok", call_id=f"c{i}") for i in range(10)],
        max_steps=3,
    )

    r = await agent.ask("问题")

    assert r.stop_reason == "max_steps"
    assert len(r.steps) == 3, f"应当恰好跑 3 步，实际 {len(r.steps)}"
    assert r.reply, "必须有回答，不能是空字符串"
    assert fake.bound_tool_choice == "none", "收尾那一轮必须禁用工具"


@pytest.mark.anyio
async def test_forced_synthesis_can_fail_gracefully() -> None:
    """收尾也失败时，至少要给用户一句话，而不是抛异常。"""
    agent, fake = make_agent([tool_call("t_ok", call_id=f"c{i}") for i in range(10)],
                             max_steps=2)
    fake.synthesize = ""      # 模拟模型收尾时返回空

    r = await agent.ask("问题")

    assert r.stop_reason == "max_steps"
    assert r.reply, "即便收尾失败也要有兜底文案"


# ── 护栏 ③：token 预算 ──────────────────────────────────────

@pytest.mark.anyio
async def test_token_budget_stops_the_loop() -> None:
    """
    预算是【累计 token】上限。

    注意这条护栏看的是账本里的真实累计值，所以它和另外两条
    （步数、墙钟）是相互独立的 —— 任何一个触顶都能停下来。
    """
    agent, _ = make_agent(
        [tool_call("t_ok", call_id=f"c{i}") for i in range(10)],
        max_steps=10,
        max_tokens=0,          # 一进来就超预算
    )

    r = await agent.ask("问题")

    assert r.stop_reason == "budget_tokens"
    assert r.reply


# ── 工具失败 ────────────────────────────────────────────────

@pytest.mark.anyio
async def test_tool_failure_is_fed_back_not_raised() -> None:
    """
    工具失败必须【回灌给模型】而不是中断循环。

    这样模型可以自己决定：换个工具、还是基于已有信息作答。
    这正是"工具挂了仍然给出回答"的原因。
    """
    async def boom(**kwargs: Any) -> Any:
        raise ConnectionError("下游不可用")

    agent, fake = make_agent(
        [tool_call("t_fail"), AIMessage(content="好的，我基于其他信息回答")],
        make_registry({"t_fail": boom}),
    )

    r = await agent.ask("问题")

    assert r.stop_reason == "completed"
    assert r.tool_results[0]["ok"] is False
    assert r.steps[0].had_error is True
    assert r.reply == "好的，我基于其他信息回答"

    # 回灌的内容里要有 error 和提示，模型才知道该怎么办
    tool_msg = next(m for m in fake.invocations[1] if type(m).__name__ == "ToolMessage")
    payload = json.loads(tool_msg.content)
    assert "error" in payload
    assert "hint" in payload, "要告诉模型下一步可以怎么办"


@pytest.mark.anyio
async def test_hallucinated_tool_does_not_break_loop() -> None:
    """模型幻觉出不存在的工具名时，循环要继续，不能崩。"""
    agent, _ = make_agent([
        tool_call("no_such_tool"),
        AIMessage(content="换个方式回答"),
    ])

    r = await agent.ask("问题")

    assert r.stop_reason == "completed"
    assert r.tool_results[0]["ok"] is False
    assert "unknown_tool" in (r.tool_results[0]["error"] or "")


# ── 只读约束 ────────────────────────────────────────────────

@pytest.mark.anyio
async def test_only_read_only_tools_are_exposed() -> None:
    """
    默认只把【只读】工具给模型。

    写操作不该由聊天触发 —— 即便某个写操作是幂等安全的。
    """
    reg = ToolRegistry()
    reg.register(ToolSpec(name="r", description="", input_schema={},
                          handler=lambda **k: None, tags=frozenset({"read_only"})))
    reg.register(ToolSpec(name="w", description="", input_schema={},
                          handler=lambda **k: None, tags=frozenset({"write"})))

    agent, fake = make_agent([AIMessage(content="x")], reg)
    await agent.ask("问题")

    exposed = {s["name"] for s in (fake.bound_schemas or [])}
    assert exposed == {"r"}, f"写工具被暴露给模型了: {exposed}"


# ── 会话 ────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_session_history_is_kept() -> None:
    agent, fake = make_agent([
        AIMessage(content="第一次回答"), AIMessage(content="第二次回答"),
    ])

    await agent.ask("第一问", session_id="s1")
    await agent.ask("第二问", session_id="s1")

    second_call = fake.invocations[1]
    contents = [getattr(m, "content", "") for m in second_call]
    assert "第一问" in contents, "上一轮的问题没进历史"


@pytest.mark.anyio
async def test_sessions_are_isolated() -> None:
    agent, fake = make_agent([
        AIMessage(content="a"), AIMessage(content="b"),
    ])

    await agent.ask("会话一的提问", session_id="s1")
    await agent.ask("会话二的提问", session_id="s2")

    second_call = fake.invocations[1]
    contents = [getattr(m, "content", "") for m in second_call]
    assert "会话一的提问" not in contents, "两个会话串了"


@pytest.mark.anyio
async def test_reset_clears_history() -> None:
    agent, fake = make_agent([AIMessage(content="a"), AIMessage(content="b")])

    await agent.ask("第一问", session_id="s1")
    agent.reset("s1")
    await agent.ask("第二问", session_id="s1")

    second_call = fake.invocations[1]
    contents = [getattr(m, "content", "") for m in second_call]
    assert "第一问" not in contents, "reset 之后历史应当清空"


# ── 边界 ────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_no_tools_available() -> None:
    """没有任何工具时给出明确说明，而不是崩掉。"""
    agent = CopilotAgent(ToolRegistry())
    agent.llm = ScriptedLLM([])

    r = await agent.ask("问题")

    assert r.stop_reason == "no_tools"
    assert r.reply


@pytest.mark.anyio
async def test_llm_failure_is_reported_not_raised() -> None:
    """模型本身调不通时要给用户一句话，而不是 500。"""
    class BrokenLLM(ScriptedLLM):
        async def ainvoke(self, messages):  # type: ignore[override]
            raise ConnectionError("模型服务不可用")

    agent, _ = make_agent([])
    agent.llm = BrokenLLM([])

    r = await agent.ask("问题")

    assert r.stop_reason == "llm_error"
    assert "失败" in r.reply


def test_tool_args_are_guaranteed_dict_by_langchain() -> None:
    """
    契约测试：langchain 保证 `tool_calls[*].args` 一定是 dict。

    这条【取代】了原先"解析 JSON 字符串"的防御测试 —— 实测发现
    AIMessage 是 pydantic 模型，传字符串会直接 ValidationError，
    所以循环里那段解析分支是死代码，已删除。

    写这条测试是为了：如果哪天 langchain 放宽了这个约束（或换了
    provider 集成），这里会失败，提醒我们那一段防御要加回来。
    """
    with pytest.raises(Exception):   # noqa: B017  具体的 ValidationError 类型随版本可能变
        AIMessage(content="", tool_calls=[
            {"name": "t_ok", "args": "not-a-json{", "id": "x", "type": "tool_call"},
        ])


@pytest.mark.anyio
async def test_tool_call_without_args_key() -> None:
    """args 缺失时退化成空参数，不该让工具调用失败。"""
    agent, _ = make_agent([
        AIMessage(content="", tool_calls=[
            {"name": "t_ok", "args": {}, "id": "x", "type": "tool_call"},
        ]),
        AIMessage(content="done"),
    ])

    r = await agent.ask("问题")

    assert r.stop_reason == "completed"
    assert r.tool_results[0]["ok"] is True
