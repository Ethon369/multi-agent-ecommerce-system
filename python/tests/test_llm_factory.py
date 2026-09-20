"""
harness.llm 工厂的测试 —— 全部离线，不发起任何 API 调用。

这些断言锁死的是实测得出的调优结论，防止有人"顺手"改回去：
    deepseek 系模型默认开推理，output 里 98.5% 是 reasoning token，
    延迟与思考量的相关系数 0.997，且 max_tokens 被忽略。
    对确定性任务关掉推理可省 89% 延迟（9198ms -> 981ms），输出逐项一致。
"""

from __future__ import annotations

import pytest

from harness import build_chat_model


def _extra_body(model) -> dict:
    return getattr(model, "extra_body", None) or {}


def test_deterministic_agent_has_thinking_disabled() -> None:
    """product_rec 只做"从候选里挑 N 个 ID"，不该为推理付延迟。"""
    model = build_chat_model("product_rec", temperature=0.3, max_tokens=512)
    assert _extra_body(model) == {"thinking": {"type": "disabled"}}


def test_creative_agent_keeps_thinking() -> None:
    """营销文案是创作型任务，保留推理 —— 由配置的例外名单控制。"""
    model = build_chat_model("marketing_copy", temperature=0.9, max_tokens=2048)
    assert "thinking" not in _extra_body(model), (
        "marketing_copy 在 llm_thinking_exempt_agents 名单里，不该被关推理"
    )


def test_explicit_override_wins() -> None:
    """显式传参优先于配置，便于测试与单次覆盖。"""
    off = build_chat_model("product_rec", temperature=0.3, max_tokens=512,
                           disable_thinking=False)
    assert "thinking" not in _extra_body(off)

    on = build_chat_model("marketing_copy", temperature=0.9, max_tokens=2048,
                          disable_thinking=True)
    assert _extra_body(on) == {"thinking": {"type": "disabled"}}


def test_passes_through_core_params() -> None:
    """工厂不能把调用方传的核心参数弄丢。"""
    model = build_chat_model("user_profile", temperature=0.3, max_tokens=1024)
    assert model.temperature == 0.3
    assert model.max_tokens == 1024


def test_agents_do_not_construct_chatopenai_directly() -> None:
    """
    回归守卫：三个 Agent 必须走工厂，而不是各自 new ChatOpenAI。

    否则新增的 provider 级调优（关推理）会漏掉它们，而且
    M3 的 token 记账也会漏 —— 这正是当初把它们收拢的原因。
    """
    import pathlib
    import re

    agents_dir = pathlib.Path(__file__).resolve().parent.parent / "agents"
    offenders = []
    for path in agents_dir.glob("*_agent.py"):
        src = path.read_text(encoding="utf-8")
        # 去掉注释行再找，避免把说明文字里的 ChatOpenAI(...) 也算进去
        code = "\n".join(
            line for line in src.splitlines() if not line.strip().startswith("#")
        )
        if re.search(r"\bChatOpenAI\s*\(", code):
            offenders.append(path.name)
    assert not offenders, (
        f"这些 Agent 绕过了 harness.build_chat_model 直接构造 ChatOpenAI: {offenders}"
    )
