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

    # Agent 超时（秒）——【整个 run() 的总预算】，不是单次尝试的预算。
    #
    # 这些值此前是【死配置】：BaseAgent.run 里从没读过 self.timeout。
    # 打开真超时之前先按实测重标定了一次（2026-09-20，5 次请求）：
    #
    #   agent              实测 min   实测 max   超时    余量
    #   user_profile          845ms     1106ms   5.0s    4.5x
    #   product_rec           390ms      773ms   8.0s   10.4x
    #   marketing_copy       2411ms    11888ms  25.0s    2.1x   <- 见下
    #   inventory             0.1ms      0.1ms   5.0s      —
    #
    # marketing_copy 原配 10.0s，比实测最大值 11.9s 还【小】—— 直接打开超时
    # 会让它偶发降级。它是唯一保留推理的 Agent（创作型任务），方差因此偏大，
    # 所以给到 25.0s（约 2 倍实测最大值）。
    #
    # 注意：若发生重试，最坏情况是 2 次尝试 + 退避。期限是总预算，
    # 所以到期就切 —— 这正是"运维写在配置里的数字必须为真"的含义。
    agent_timeout_user_profile: float = 5.0
    agent_timeout_product_rec: float = 8.0
    agent_timeout_marketing_copy: float = 25.0
    agent_timeout_inventory: float = 5.0

    # 熔断器（按 agent 名索引，进程级共享）
    # 窗口内失败计数 >= 阈值即触发，而不是累计错误率 —— 后者单调不降，
    # 会让长跑进程一旦出错就永远回不来。
    breaker_failure_threshold: int = 5
    breaker_window: int = 20
    breaker_reset_timeout_s: float = 30.0

    # ── MCP ────────────────────────────────────────────────────
    # 两个开关都【默认 false】。理由：这是一条新引入的外部依赖，
    # 默认关闭 + 显式启用，保证现有链路零破坏（也就是零回归风险）。
    # 打开后 MCP 挂掉也必须能降级 —— 见 mcp_wms_timeout 的注释。

    # 是否注册 recommend_server 的工具（把项目能力暴露给外部 MCP Host）
    mcp_enabled: bool = False

    # 是否让库存 Agent 通过 MCP 客户端去查 WMS（而不是读 Product.stock）
    mcp_wms_enabled: bool = False

    # SQLite 库存库路径。相对路径的基准是【python/ 目录】，
    # 因为 config/settings.py 的 env_file=".env" 也是相对 cwd 解析的。
    mcp_wms_db_path: str = "./wms.db"

    # MCP 调用超时（秒）。
    # 必须【小于】 agent_timeout_inventory(5.0) —— 这样 MCP 先超时，
    # 库存 Agent 还有余量走 fallback 返回 Product.stock，而不是自己先被切断。
    # 库存查询是毫秒级操作，3 秒不给响应说明进程已异常，继续等只会拖慢主链路。
    mcp_wms_timeout: float = 3.0

    model_config = {"env_file": ".env", "env_prefix": "ECOM_"}


@lru_cache()
def get_settings() -> Settings:
    return Settings()
