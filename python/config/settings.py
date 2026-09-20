from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    app_name: str = "Multi-Agent E-Commerce System"
    debug: bool = False

    # LLM
    llm_api_key: str = ""
    llm_base_url: str = "https://api.minimax.chat/v1"
    llm_model: str = "MiniMax-M1"
    llm_temperature: float = 0.7
    llm_max_tokens: int = 2048

    # 推理开关（provider 相关，目前只对 DeepSeek 系生效）。
    #
    # 实测背景（2026-09-20，deepseek-flash @ api.deepseek.com）：
    #   - 该模型默认【开启推理】，output token 里 98.5% 是 reasoning token
    #   - 延迟与 reasoning token 数的相关系数 r = 0.997 —— 延迟几乎完全由"想多久"决定
    #   - max_tokens 参数被【完全忽略】：设成 64 反而生成了 2797 个 token
    #   - 对 rerank 这类确定性任务，推理只贡献延迟不贡献质量：
    #     关掉后 9198ms -> 981ms（-89%），返回的商品 ID 逐项一致
    #
    # 所以默认关掉。但创作型任务（营销文案）保留推理 —— 见下面的例外名单。
    llm_disable_thinking: bool = True
    # 例外名单：逗号分隔的 agent 名，这些保留推理。
    llm_thinking_exempt_agents: str = "marketing_copy"

    # Redis
    redis_url: str = "redis://localhost:6379/0"
    feature_ttl_seconds: int = 86400

    # Milvus
    milvus_host: str = "localhost"
    milvus_port: int = 19530
    milvus_collection: str = "product_embeddings"

    # Database
    database_url: str = "sqlite:///./ecommerce.db"

    # A/B Testing
    ab_test_enabled: bool = True
    ab_test_default_bucket_count: int = 100

    # Agent timeouts (seconds)
    agent_timeout_user_profile: float = 5.0
    agent_timeout_product_rec: float = 8.0
    agent_timeout_marketing_copy: float = 10.0
    agent_timeout_inventory: float = 5.0

    model_config = {"env_file": ".env", "env_prefix": "ECOM_"}


@lru_cache()
def get_settings() -> Settings:
    return Settings()
