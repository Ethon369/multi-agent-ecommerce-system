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
    #
    # ⚠️ 这里发生过一次【基于证据的结论反转】，过程值得记下来：
    #
    #   最初的判断：营销文案是创作型任务，思考可能换来更好的文案，
    #              所以把 marketing_copy 放进例外名单（保留推理）。
    #
    #   建好评测集之后实测（10 条用例，改一个环境变量跑两遍）：
    #     通过率      100%  -> 100%      <- 确定性断言抓不到质量变化
    #     延迟 p50    8030  -> 2788 ms   （2.9 倍）
    #     延迟 p95   14880  -> 3997 ms   （3.7 倍）
    #     平均成本  $0.002495 -> $0.000608（4.1 倍）
    #     输出 token   1720  -> 318      （5.4 倍）
    #
    #   用 LLM 裁判（不同模型，deepseek-v4-pro）评文案质量：
    #     保留推理 {3.5, 3, 4}  关推理 {4, 4, 3.5}
    #     -> 关推理在 3 条里 2 条更高，但每条只有 1-2 个样本，是噪声
    #
    #   结论：质量差异【在本样本量下不可测】，而性能与成本收益是确定的。
    #         证据对质量不确定、对成本确定时，选成本低的一侧，保留回退开关。
    #
    # 想恢复"文案保留推理"（代价是 2.9 倍延迟、4.1 倍成本）：
    #     ECOM_LLM_THINKING_EXEMPT_AGENTS=marketing_copy
    llm_thinking_exempt_agents: str = ""

    # ── Redis 实时特征 ──────────────────────────────────────────
    # 开关默认 false：和 MCP 同一个理由 —— 新引入的外部依赖，
    # 默认关闭 + 显式启用，保证现有链路零破坏。
    #
    # 打开前先灌行为数据，否则每个用户都是"读到了但没数据"：
    #     python scripts/seed_behavior.py --reset

    # 是否让用户画像 Agent 去 Redis 读实时行为特征（而不是用内置兜底值）
    feature_store_enabled: bool = False

    # Redis 连接串。两个坑，都是实测踩出来的：
    #
    # ⚠️ 用 `127.0.0.1`，不要用 `localhost`。
    #    `localhost` 会先解析到 IPv6 的 `::1`，而本机 Redis 只监听 IPv4 ——
    #    连接先在 `::1` 上挂约 2 秒才回落。实测首次 PING：
    #         localhost   2052.3 ms      127.0.0.1   2.4 ms
    #    这个 2 秒正好大于请求路径的超时，会把连接掐断并造成【永久降级】。
    #
    # ⚠️ 本机 6379 上是一个【原生 Redis 5.0】，它不支持 HELLO 命令，而
    #    redis-py 5+ 默认走 RESP3、建连时会先发 HELLO —— 所以客户端必须
    #    显式指定 protocol=2（见 services/feature_store.py）。
    #    RESP2 在 Redis 7.x 上同样合法，这不是"只在本机能跑的 hack"。
    redis_url: str = "redis://127.0.0.1:6379/0"

    # 特征读取超时（秒）。
    # 必须【小于】 agent_timeout_user_profile(5.0) —— 让 Redis 先超时，
    # 画像 Agent 还有余量走 fallback，而不是自己先被 wait_for 切断。
    feature_store_timeout_s: float = 0.5

    # 特征窗口（天）。滑动窗口由【读取时的 score 区间】强制，
    # 这个值只决定 purchase_count 的统计窗口宽度。
    feature_window_days: int = 30

    # GC 用的 key 过期时间（秒），默认 30 天。
    #
    # ⚠️ TTL 不是窗口。EXPIRE 每次写入都会被刷新，所以它只对
    # "再也不来的用户"生效 —— 活跃用户的 ZSET 依然会无界增长。
    # 真正阻止增长的是每次写入时的 ZREMRANGEBYSCORE 修剪（见 feature_store.py）。
    # 这个 TTL 只是最后一道兜底。
    feature_ttl_seconds: int = 2592000

    # 计算"活跃时段"用的时区偏移（小时）。
    #
    # 必须显式固定：Dockerfile 是 python:3.12-slim 且没有 ENV TZ，
    # 容器里是 UTC、本机是中国时区 —— 不固定的话同一个用户在容器里
    # 会算出差 8 小时的 active_hours，而 LLM 照样拿它当"活跃时段"用。
    # 中国无夏令时，固定 +08:00 是正确的，且不依赖 tzdata 这个传递依赖。
    feature_tz_offset_hours: int = 8

    # Milvus
    milvus_host: str = "localhost"
    milvus_port: int = 19530
    milvus_collection: str = "product_embeddings"

    # Database
    database_url: str = "sqlite:///./ecommerce.db"

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
