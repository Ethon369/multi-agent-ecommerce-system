"""
模型价格表 —— 把「token 数」换算成「钱」。

    核心原则：token 是事实，价格是配置。
    ────────────────────────────────────
    token 数量是从 API 响应里读出来的，是客观事实。
    而价格会变：厂商调价、促销、你换了模型、你切到了另一个 provider。

    所以：
      - 价格表可以被环境变量 / JSON 文件覆盖，不需要改代码
      - 查不到价格时返回 None，**绝不猜**
      - None 会一路传播到响应里（cost_usd: null, cost_known: false），
        而不是显示一个看起来很确定的错误数字

    一个看起来精确但其实是编的成本数字，比没有成本数字更危险 ——
    你会拿它做决策。

    价格随「时段」浮动（peak / off-peak）怎么办？
    ────────────────────────────────────────────
    这里取【峰值价】，也就是最贵的那一档。理由：
    估算成本时宁可高估 —— 高估会让你更谨慎，低估会让你超预算。
    DeepSeek 的定价规则（2026-09-20 查证）：off-peak 是 peak 的一半，
    peak 时段为 UTC 周一至周五 01:00-04:00 与 06:00-10:00。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

import structlog

logger = structlog.get_logger()

# 价格表的记录时间。价格会变，所以要能一眼看出这个表有多旧。
PRICING_AS_OF = "2026-09-20"
PRICING_SOURCE = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"


@dataclass(frozen=True)
class ModelPrice:
    """单位：美元 / 每百万 token。"""

    input_per_1m: float
    output_per_1m: float
    cached_input_per_1m: float = 0.0

    def cost(self, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> float:
        """
        计算一次调用的成本。

        注意：缓存命中的输入 token 要单独计价 —— 实测这个差价高达 50 倍
        （deepseek-flash 命中 $0.003 vs 未命中 $0.15 每百万 token），
        所以只用一个 input 单价会把成本算得严重偏高。
        """
        uncached = max(0, input_tokens - cached_tokens)
        return (
            uncached / 1_000_000 * self.input_per_1m
            + cached_tokens / 1_000_000 * self.cached_input_per_1m
            + output_tokens / 1_000_000 * self.output_per_1m
        )


# 内置默认表。取【峰值价】（最贵档），理由见模块 docstring。
#
# ⚠️ 换 provider / 换模型时一定要更新这里，或者用 ECOM_PRICING_JSON 覆盖。
#    查不到价格的模型会返回 None，响应里显示 cost_usd: null —— 这是有意的，
#    不是 bug。看到一个 null 比看到一个编的数字好。
DEFAULT_PRICING: dict[str, ModelPrice] = {
    "deepseek-flash": ModelPrice(
        input_per_1m=0.30,
        output_per_1m=1.20,
        cached_input_per_1m=0.006,
    ),
    "deepseek-v4-pro": ModelPrice(
        input_per_1m=1.32,
        output_per_1m=3.96,
        cached_input_per_1m=0.044,
    ),
}


class PricingTable:
    """
    按【模型名前缀】匹配的价格表，最长前缀优先。

    为什么要前缀匹配：模型名常带日期或版本后缀
    （`deepseek-flash` / `deepseek-flash-2026-08`），
    精确匹配会导致"明明配了价格却查不到"，然后静默变成 None。
    """

    def __init__(self, prices: dict[str, ModelPrice] | None = None) -> None:
        self._prices = dict(DEFAULT_PRICING)
        if prices:
            self._prices.update(prices)

    @classmethod
    def from_env(cls) -> "PricingTable":
        """
        构造顺序（后者覆盖前者）：
            内置默认表 -> ECOM_PRICING_FILE 指向的 JSON -> ECOM_PRICING_JSON 内联 JSON
        """
        table = cls()

        path = os.environ.get("ECOM_PRICING_FILE")
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    table._merge(json.load(f))
            except Exception as exc:
                logger.error("pricing.file_load_failed", path=path, error=str(exc))

        inline = os.environ.get("ECOM_PRICING_JSON")
        if inline:
            try:
                table._merge(json.loads(inline))
            except Exception as exc:
                logger.error("pricing.inline_load_failed", error=str(exc))

        return table

    def _merge(self, raw: dict) -> None:
        for model, spec in (raw or {}).items():
            if not isinstance(spec, dict):
                continue
            try:
                self._prices[model] = ModelPrice(
                    input_per_1m=float(spec["input"]),
                    output_per_1m=float(spec["output"]),
                    cached_input_per_1m=float(spec.get("cached_input", 0.0)),
                )
            except (KeyError, TypeError, ValueError) as exc:
                logger.error("pricing.bad_entry", model=model, error=str(exc))

    def lookup(self, model: str) -> ModelPrice | None:
        if not model:
            return None
        if model in self._prices:
            return self._prices[model]

        # 最长前缀优先，避免 "deepseek" 这种短前缀抢走更精确的匹配
        matches = [name for name in self._prices if model.startswith(name)]
        if matches:
            return self._prices[max(matches, key=len)]

        logger.warning("pricing.unknown_model", model=model)
        return None

    def cost(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int = 0,
    ) -> float | None:
        """返回美元成本；查不到价格返回 None（**不猜**）。"""
        price = self.lookup(model)
        if price is None:
            return None
        return price.cost(input_tokens, output_tokens, cached_tokens)

    def fingerprint(self) -> str:
        """
        价格表的指纹。

        为什么需要：对比"优化前 vs 优化后"的成本时，如果两次用的价格表不同，
        那这个对比就是假的。所以评测报告里要记下这个指纹。
        """
        import hashlib

        canonical = json.dumps(
            {k: [v.input_per_1m, v.output_per_1m, v.cached_input_per_1m]
             for k, v in sorted(self._prices.items())},
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()[:12]
