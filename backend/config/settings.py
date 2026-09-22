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

    # ── 接入层 ──────────────────────────────────────────────────
    #
    # 【已删除】原先这里还有 milvus_host / milvus_port / milvus_collection
    # 和 database_url 四项配置。删掉的理由和 harness/trace.py 里删 span() 一样：
    # grep 生产代码 0 引用（`pymilvus` / `sqlalchemy` 全仓库都没有 import）。
    # 留着它们的实际代价不是几行代码，而是【误导】—— 面试官看到 milvus_host
    # 会问"向量库用的什么索引"，正确答案"其实没接"是最难看的答案。
    # 真要做向量召回时再加，那时它会和真实的客户端代码一起进来。

    # 日志格式。true = 逐行 JSON（生产/采集用），false = 彩色控制台（本地读着舒服）。
    #
    # 这个开关存在的原因：项目此前【从未】调用 structlog.configure() ——
    # 原来的 configure_logging() 在"删死代码"那一轮被删了（当时确实 0 引用），
    # 结果所有日志走 structlog 默认的彩色 ConsoleRenderer，而文档里写的是
    # "结构化 JSON 日志"。两者不符。现在真的接上了，并留一个关掉的口子。
    log_json: bool = True

    # 日志级别。DEBUG / INFO / WARNING / ERROR。
    #
    # 为什么它是配置项而不是硬编码 INFO：过滤级别是【全局的】，
    # 设成 INFO 之后生产代码里那 3 处 logger.debug 就再也不输出了。
    # 生产正要如此，但测试需要放回来 —— 否则"某个 debug 事件到底有没有发"
    # 这类问题在测试里无法回答。tests/conftest.py 因此把它设成 DEBUG。
    # 认不出的级别名会被兜底成 INFO，不会让服务起不来。
    log_level: str = "INFO"

    # `python main.py` 启动时是否开 uvicorn 热重载。**默认 false。**
    #
    # 原先 main.py 的 __main__ 里硬编码 reload=True，而它和本项目的
    # MCP 设计是冲突的：reloader 重启时会杀掉整个子进程树，MCP 的
    # stdio 子进程会先死，症状是静默挂起或 BrokenPipe（CLAUDE.md 陷阱 5）。
    # 容器里更不需要热重载。所以改成显式开启，默认走安全的那一侧。
    #
    # 本地改代码时想要热重载：ECOM_DEV_RELOAD=true
    dev_reload: bool = False

    # API Key 鉴权。**默认 false** —— 和 MCP / 特征层同一个口径：
    # 新增的准入控制默认不启用，保证现有链路零破坏
    # （尤其是 eval/runner.py 走 HTTP 调服务做评测，不能因为加了鉴权就集体 401）。
    api_key_enabled: bool = False
    api_key: str = ""

    # 免鉴权路径（逗号分隔，精确匹配）。/health 必须在里面 ——
    # 探活被鉴权挡在门外，会让编排系统认为服务已经死了。
    api_key_exempt_paths: str = "/health,/docs,/openapi.json,/redoc"

    # CORS 允许来源（逗号分隔）。默认给的是【开发期端口】，不是 "*"。
    #
    # 为什么不再用 "*"：通配符在生产是不可接受的，而它恰恰是"忘了改"的
    # 最常见形态 —— 默认值给成安全的那一侧，才不用靠人记得。
    # 5173 = Vite dev，4173 = Vite preview；换端口时在这里追加。
    # 需要临时放开成通配符时，显式写 ECOM_CORS_ALLOW_ORIGINS=* 即可。
    cors_allow_origins: str = (
        "http://localhost:5173,http://127.0.0.1:5173,"
        "http://localhost:4173,http://127.0.0.1:4173"
    )

    # ── 限流 ────────────────────────────────────────────────────
    #
    # **默认 false**，同 api_key_enabled 的理由：新能力默认不启用，
    # 保证现有链路零破坏（尤其 eval/runner.py 的 12 条用例是串行快速
    # 连打的，默认开启会把它卡在 429 上，而症状看起来像"评测挂了"）。
    #
    # 为什么这个开关值得存在（而不是"演示项目用不上"）：
    # 推荐接口单次约 3 秒、成本约 $0.0005（实测）。一个忘加 sleep 的
    # for 循环能在几分钟内烧掉可观额度，而服务本身不会给出任何信号。
    rate_limit_enabled: bool = False

    # 窗口长度（秒）内允许的请求数。
    # 默认给得很宽（60 次/分钟）—— 限流第一版的目标是拦住"失控的循环"，
    # 不是把正常调用方卡住。真正调参要基于实测流量，而那需要真实流量。
    rate_limit_requests: int = 60
    rate_limit_window_s: float = 60.0

    # 免限流路径（逗号分隔，精确匹配）。
    # /health 与 /ready 必须在里面：探针被限流挡住，编排系统会认为
    # 实例挂了并把它摘掉 —— 而它其实好好的。
    rate_limit_exempt_paths: str = "/health,/ready,/docs,/openapi.json,/redoc"

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

    @property
    def cors_origins(self) -> list[str]:
        """
        把逗号分隔的来源串解析成列表。

        单独留一个 `*` 的分支，而不是"原样 split 就行"：Starlette 的
        CORSMiddleware 对 `["*"]` 和 `["*", "http://x"]` 的处理【不一样】——
        后者会被当成一组普通来源去精确匹配，等于通配符静默失效。
        把这两种语义在这里分开，调用方就不用知道这个区别。
        """
        items = [x.strip() for x in self.cors_allow_origins.split(",") if x.strip()]
        if "*" in items:
            return ["*"]
        return items

    @staticmethod
    def _split_csv(value: str) -> set[str]:
        """逗号分隔串 → 去空白的集合。鉴权与限流的免检名单共用。"""
        return {x.strip() for x in value.split(",") if x.strip()}

    @property
    def api_key_exempt_set(self) -> set[str]:
        """免鉴权路径集合。用 set 是因为匹配是精确匹配，不是前缀匹配。"""
        return self._split_csv(self.api_key_exempt_paths)

    @property
    def rate_limit_exempt_set(self) -> set[str]:
        """免限流路径集合。口径与 api_key_exempt_set 一致。"""
        return self._split_csv(self.rate_limit_exempt_paths)

    model_config = {"env_file": ".env", "env_prefix": "ECOM_"}


@lru_cache()
def get_settings() -> Settings:
    return Settings()
