"""
LLM 裁判 —— 给营销文案的质量打主观分。

    ⚠️ 它是【非门禁】的。默认不跑，跑了也不影响通过率。

    为什么不拿它当门禁
    ────────────────
    ① 它会让评测变慢且不可复现。文案 Agent 跑在 temperature=0.9 上，
       同一份输入两次的输出不同，裁判打的分也不同 ——
       而"可重复的通过率"正是评测运行器存在的全部意义。
       引入一个自带随机性的门禁，会把整套评测的可信度拖下水。

    ② 10 条用例上的统计显著性不存在。裁判分数差 0.3 分，
       可能完全是噪声。**不要为它建显著性检验**，那是自欺。

    ③ 自我偏好：让同一个模型既写文案又评文案，它会偏向自己的输出。
       所以这里用【不同的模型】来评（见 JUDGE_MODEL）。

    那它有什么用
    ────────────
    用来回答"关掉推理之后文案是变差了还是差不多"这类问题 ——
    给一个粗略的方向感，而不是一个判决。

    这也是一个可以讲的设计判断：
    "我保留了 LLM 裁判，但让它非门禁 —— 因为它的方差会掩盖真正的回归。"

用法：
    python -m eval.judge <评测报告.json>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from pathlib import Path

import structlog
from langchain_core.messages import HumanMessage, SystemMessage

PYTHON_DIR = Path(__file__).resolve().parent.parent
if str(PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_DIR))

logger = structlog.get_logger()

# 刻意与生成文案的模型【不同】—— 同一个模型评自己会有自我偏好。
JUDGE_MODEL = "deepseek-v4-pro"

JUDGE_PROMPT = """你是一个电商营销文案的评审。给下面每条文案打分（1-5 分）。

评分维度：
- 相关性：文案是否贴合该商品的具体特征（而不是放之四海皆准的套话）
- 自然度：中文是否通顺自然，有没有生硬的堆砌感
- 说服力：是否能让人产生购买兴趣

只输出 JSON，格式：
{"scores": [{"product_id": "...", "score": 4, "reason": "一句话"}]}

待评文案：
"""


async def judge_copies(copies: list[dict[str, str]]) -> dict:
    """给一批文案打分。失败返回空结果而不是抛异常。"""
    if not copies:
        return {"scores": [], "mean": None, "model": JUDGE_MODEL}

    from config import get_settings
    from langchain_openai import ChatOpenAI

    s = get_settings()
    llm = ChatOpenAI(
        api_key=s.llm_api_key,
        base_url=s.llm_base_url,
        model=JUDGE_MODEL,
        temperature=0.0,          # 裁判要尽量稳定
        max_tokens=2048,
    )

    listing = "\n".join(
        f"- {c.get('product_id')}: {c.get('copy')}" for c in copies
    )
    try:
        resp = await llm.ainvoke([
            SystemMessage(content="你是严格的文案评审，只输出 JSON。"),
            HumanMessage(content=JUDGE_PROMPT + listing),
        ])
        raw = resp.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        data = json.loads(raw)
        scores = [
            x["score"] for x in data.get("scores", [])
            if isinstance(x.get("score"), (int, float))
        ]
        return {
            "scores": data.get("scores", []),
            "mean": round(statistics.mean(scores), 2) if scores else None,
            "model": JUDGE_MODEL,
        }
    except Exception as exc:
        logger.error("judge.failed", error=str(exc)[:200])
        return {"scores": [], "mean": None, "model": JUDGE_MODEL, "error": str(exc)[:200]}


async def main() -> int:
    parser = argparse.ArgumentParser(description="给评测报告里的文案打主观分")
    parser.add_argument("report", help="eval.runner 产出的 JSON 报告")
    parser.add_argument("--limit", type=int, default=3, help="抽几条报告里的用例来评")
    args = parser.parse_args()

    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    runs = [r for r in report.get("runs", []) if r.get("copy_count")]
    if not runs:
        print("报告里没有可评的文案（runner 默认不保存文案原文）")
        return 1

    print(f"裁判模型: {JUDGE_MODEL}   （刻意与生成模型不同，避免自我偏好）")
    print()
    for r in runs[: args.limit]:
        copies = r.get("copies_sample") or []
        if not copies:
            print(f"  {r['id']}: 报告里没有文案原文，跳过")
            continue
        result = await judge_copies(copies)
        print(f"  {r['id']}: 均分 {result['mean']}  ({len(result['scores'])} 条)")

    print()
    print("提醒：这是【非门禁】指标。它的方差较大，只用于看方向，不要拿它做回归判决。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
