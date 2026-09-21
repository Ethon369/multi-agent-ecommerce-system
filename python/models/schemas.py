from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, SerializeAsAny


class UserSegment(str, Enum):
    NEW_USER = "new_user"
    ACTIVE = "active"
    HIGH_VALUE = "high_value"
    PRICE_SENSITIVE = "price_sensitive"
    CHURN_RISK = "churn_risk"


class UserProfile(BaseModel):
    user_id: str
    age: int | None = None
    gender: str | None = None
    city: str | None = None
    segments: list[UserSegment] = Field(default_factory=list)
    preferred_categories: list[str] = Field(default_factory=list)
    price_range: tuple[float, float] = (0.0, 10000.0)
    recent_views: list[str] = Field(default_factory=list)
    recent_purchases: list[str] = Field(default_factory=list)
    rfm_score: dict[str, float] = Field(default_factory=dict)
    real_time_tags: dict[str, Any] = Field(default_factory=dict)


class Product(BaseModel):
    product_id: str
    name: str
    category: str
    price: float
    description: str = ""
    brand: str = ""
    seller_id: str = ""
    stock: int = 0
    tags: list[str] = Field(default_factory=list)
    score: float = 0.0
    image_url: str = ""


class RecommendationRequest(BaseModel):
    user_id: str
    scene: str = "homepage"
    num_items: int = 10
    context: dict[str, Any] = Field(default_factory=dict)


class AgentResult(BaseModel):
    agent_name: str
    success: bool = True
    latency_ms: float = 0.0
    error: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 1.0


class UserProfileResult(AgentResult):
    agent_name: str = "user_profile"
    profile: UserProfile | None = None


class ProductRecResult(AgentResult):
    agent_name: str = "product_rec"
    products: list[Product] = Field(default_factory=list)
    recall_strategy: str = ""


class MarketingCopyResult(AgentResult):
    agent_name: str = "marketing_copy"
    copies: list[dict[str, str]] = Field(default_factory=list)
    prompt_template_used: str = ""


class InventoryResult(AgentResult):
    agent_name: str = "inventory"
    available_products: list[str] = Field(default_factory=list)
    low_stock_alerts: list[dict[str, Any]] = Field(default_factory=list)
    purchase_limits: dict[str, int] = Field(default_factory=dict)


class HarnessUsageReport(BaseModel):
    """一次请求的 token 与成本账本。"""

    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    # cost_usd 为 None 时表示【查不到该模型的单价】，不是"免费"。
    # 这是有意的：一个看起来精确但其实是编的数字，比没有数字更危险。
    cost_usd: float | None = None
    cost_known: bool = False
    estimated: bool = False
    """True 表示至少一次用量是估算的（API 没返回 usage），不能当实测引用。"""
    by_agent: dict[str, dict[str, int]] = Field(default_factory=dict)


class HarnessReport(BaseModel):
    """
    运行时保障层的自述报告。

    刻意做成 RecommendationResponse 的【顶层字段】而不是塞进 agent_results：
    它描述的是【整个请求】的运行时状态（账本 / 熔断 / 各 Agent 耗时），
    不是某一个 Agent 的产出 —— 塞进去语义就是错的。
    """

    usage: HarnessUsageReport = Field(default_factory=HarnessUsageReport)
    agents: dict[str, dict[str, Any]] = Field(default_factory=dict)
    breakers: dict[str, dict[str, Any]] = Field(default_factory=dict)


class RecommendationResponse(BaseModel):
    request_id: str
    user_id: str
    products: list[Product] = Field(default_factory=list)
    marketing_copies: list[dict[str, str]] = Field(default_factory=list)
    experiment_group: str = "control"
    # SerializeAsAny 是为了绕开 pydantic 的一个【静默】行为：
    # 它默认按【声明类型】序列化，所以声明成基类 AgentResult 时，
    # 子类独有的字段（profile / products / copies / low_stock_alerts /
    # available_products / purchase_limits）会被直接丢掉 ——
    # 不报错、无日志。实测：ProductRecResult 的 8 个字段到这里只剩 6 个。
    #
    # 为什么不用联合类型（UserProfileResult | ProductRecResult | ...）：
    # BaseAgent._fallback() 在超时/熔断时返回的是【基类】AgentResult，
    # 联合类型会让这条降级路径校验失败 —— 故障注入的 4/4 HTTP 200 会变成 500。
    # SerializeAsAny 只改序列化、不改校验，所以降级路径不受影响。
    #
    # 附带好处：新增 Agent 时这里【不用动】，不会再复发。
    agent_results: dict[str, SerializeAsAny[AgentResult]] = Field(default_factory=dict)
    harness: HarnessReport | None = None
    total_latency_ms: float = 0.0
    timestamp: datetime = Field(default_factory=datetime.now)
