# 二次开发指南（Python 版）

> 面向"想把这份代码改成自己的东西"的场景。所有结论基于 2026-09-20 在本机的实际阅读与实测，不是照抄 README。

---

## 0. 先建安全网（5 分钟，别跳）

改之前先让自己"随时能回退、改完能验证"：

```bat
:: 1) 提交一个干净的 baseline
cd /d D:\devlop\multi-agent-ecommerce-system
git add -A
git commit -m "baseline: 依赖安装完成，服务可启动"

:: 2) 确认 API Key 没有被提交（.env 已在 .gitignore 里，git status 不应出现它）
git status --short
type .gitignore | findstr /i env

:: 3) 补上测试依赖（requirements.txt 里没有 pytest）
cd python
.venv\Scripts\python.exe -m pip install pytest -i https://mirrors.aliyun.com/pypi/simple
.venv\Scripts\python.exe -m pytest tests\ -v
```

**把接口探测当回归脚本用。** 归档目录里的 `probe_recommend.py` 会打印 4 个 Agent 的 `success` 与 `latency`，每次改动后跑一次：

```bat
cd /d D:\devlop\_archive\probes_20260920
D:\devlop\multi-agent-ecommerce-system\python\.venv\Scripts\python.exe probe_recommend.py 8000
```

**固定解释器。** 在 IDE（PyCharm / VS Code）里把项目解释器指定为
`D:\devlop\multi-agent-ecommerce-system\python\.venv\Scripts\python.exe`，就不会再出现"包明明装了却 import 不到"的 venv 串台问题。

---

## 1. 结构地图：一条请求怎么走

```
POST /api/v1/recommend                    main.py:73
  └─ SupervisorOrchestrator.recommend()   orchestrator/supervisor.py:56
       ├─ Phase 1（并行 asyncio.gather，:70）
       │    ├─ UserProfileAgent    agents/user_profile_agent.py
       │    └─ ProductRecAgent     agents/product_rec_agent.py
       ├─ Phase 2（并行，:91）
       │    ├─ ProductRecAgent（重排，带用户画像）
       │    └─ InventoryAgent      agents/inventory_agent.py
       ├─ 库存过滤 + 截断 TopN      :98
       └─ Phase 3（串行，:104）
            └─ MarketingCopyAgent  agents/marketing_copy_agent.py
```

**所有 Agent 都继承 `agents/base_agent.py` 的 `BaseAgent`，只需实现 `_execute()`** —— 超时（`asyncio.wait_for`）、指数退避重试（tenacity）、失败降级（`_fallback`）已在 `run()` 里封装好。

配套服务：

| 文件 | 作用 | 当前状态 |
|---|---|---|
| `services/ab_test.py` | 流量分桶 + Thompson Sampling | 可用，`/api/v1/experiments` 可查 |
| `services/metrics.py` | Agent/业务指标统计 | 可用，但**未暴露 Prometheus `/metrics` 端点** |
| `orchestrator/graph.py` | LangGraph 状态图版编排 | 可用，走 `/api/v1/recommend/graph` |

> `services/feature_store.py`（Redis 实时特征，117 行）**曾存在但从未被实例化**，
> 已于 2026-09-21 的死代码清理中删除。想接真实行为源，见下面第 2 节。

---

## 2. 代码里"已预留但没接线"的钩子 —— 二次开发的最佳切入点

这些地方作者都留了 `# injected in Phase 2` 或 `# Phase 2: ...` 注释，是设计好等你填的：

| 位置 | 现状 | 你需要做什么 |
|---|---|---|
| `agents/user_profile_agent.py` 的 `_collect_behavior()` | 直接返回**写死的演示行为数据**（曾有一个指向 Redis 的分支，因那个模块从未被实例化，已随死代码清理一并删除） | 在 `_collect_behavior()` 里换成一次真实查询 —— 只改这一处。注意用 `redis.asyncio`，见"坑 2" |
| `agents/product_rec_agent.py:74` `self.vector_store = None`；`_recall()` 里 `if self.vector_store: pass`（:105） | 召回走写死的 `MOCK_PRODUCTS`（15 条） | 实现向量检索 / 接 Milvus，替换 mock |
| `agents/inventory_agent.py:30` `self.db = None`；`_check_stock()` 里 `if self.db: pass`（:82） | 库存直接读 `Product.stock` | 接真实库存表 |
| `config/settings.py:26` `database_url` | **没有任何 SQLAlchemy 引擎、没有建表脚本** | 用数据库得从零接 |

---

## 3. 改动难度分级（按风险从低到高）

### L1 改配置就能见效（零风险）

- **换模型 / 温度 / 超时**：`.env`（`ECOM_LLM_*`）+ `config/settings.py`
- **营销文案模板**：`agents/marketing_copy_agent.py` 的 `PROMPT_TEMPLATES`（:27-47），每个 `UserSegment` 一个模板，加模板 = 加一个键
- **违规词表**：同文件 `FORBIDDEN_WORDS`（:49-52）
- **库存阈值 / 限购策略**：`agents/inventory_agent.py:16-18`
- **商品数据**：`agents/product_rec_agent.py` 的 `MOCK_PRODUCTS`（:41-57）

### L2 新增一个 Agent（推荐作为第一个动手项）

最能体现"你懂这个架构"的改动，5 步：

1. `models/schemas.py`：新增 `XxxResult(AgentResult)`（带自己的业务字段）
2. `agents/xxx_agent.py`：继承 `BaseAgent`，实现 `async def _execute(**kwargs)`
3. `agents/__init__.py`：导出新 Agent
4. `orchestrator/supervisor.py`：在合适的 Phase 用 `asyncio.gather` 挂上去
5. 指标收集自动生效（`main.py:_collect_metrics` 是遍历 `response.agent_results` 的）

✅ **第 6 步现在不需要额外操作**（2026-09-21 已修）：`agent_results` 声明为
`dict[str, SerializeAsAny[AgentResult]]`，序列化按【运行时真实类型】走，
新 Agent 的产出会自动出现在响应里。

> 修之前这里是个坑：声明成基类 `AgentResult` 时，pydantic 按【声明类型】序列化，
> 子类独有字段被静默截断 —— 实测 `ProductRecResult` 的 8 个字段进到响应里只剩 6 个。
>
> **不要改回裸的 `dict[str, AgentResult]`，也不要改成联合类型** ——
> 后者会让 `BaseAgent._fallback()` 返回的基类结果校验失败（超时/熔断时
> 从 HTTP 200 变成 500）。回归测试：`tests/test_response_schema.py`。

### L3 落地数据层（最能体现工程完整度）

- 接 SQLAlchemy + SQLite/MySQL，把 `MOCK_PRODUCTS` 换成真实表（含分页、索引、事务边界）
- 在 `user_profile_agent._collect_behavior()` 里接真实行为源，实现滑窗特征与 RFM
- 要点：连接池、异常时的降级路径、写操作的幂等

### L4 架构级

- 把手写 `asyncio.gather` 换成 `orchestrator/graph.py` 的 LangGraph 图（两套现已并存，可直接对比）
- 加 LangGraph Checkpoint 做**断点续跑 / human-in-the-loop**
- 接 Milvus 做真实向量召回

---

## 4. 已实测的坑（照着避）

1. ~~**`agent_results` 子类字段被截断**~~ —— ✅ **已修**（2026-09-21，见 L2 第 6 步）。
   踩坑记录保留：pydantic 按【声明类型】序列化，声明成基类就会静默丢掉子类字段。
2. **接 Redis 必须用异步客户端。** 调用处是 `await`，若用 `from redis import Redis`（同步）会直接 `TypeError`：
   ```python
   from redis.asyncio import Redis
   client = Redis.from_url(settings.redis_url, decode_responses=True)
   ```
3. **`/api/v1/experiments/{experiment_id}/outcome` 的 `group`、`success` 是 query 参数**，不是 JSON body（`main.py:136`），用 curl 传 body 会 422。
4. **`main.py` 里 `reload=True` 只适合开发**，自建部署要去掉（它会额外起一个 reloader 进程）。
5. **README 的性能数字不可信**：README 称"目标 P99 < 2000 ms"、"延迟优化到 2s"，
   而实测接真实 LLM 后全链路是 **15.8 ~ 16.6 s**。
   > 后续：二次开发时把延迟优化到 **p50 2,788 ms / p95 3,997 ms**（约 17 倍），
   > 但**仍然不是 2 秒**。README 现已修正，并新增了「哪些数字能信」一节。（profile 7.97s + rerank 4.13s + copy 3.74s，三阶段串行累加）。**面试或简历里只写自己测出来的数**。
6. **`requirements.txt` 缺 pytest**，`tests/` 默认跑不了（只有一个 `test_ab_test.py`）。
7. **docker-compose 里的 Redis / Milvus / MySQL 当前代码都没用到**，只想跑通不必起容器。
8. **三语言实现共享同一套架构设计，但代码是各自独立的**，改 Python 版不会同步到 Java / Go。
9. **CMD 默认 GBK**：用 curl 传含中文的 JSON 会乱码导致 422 → 改用 `http://localhost:8000/docs` 页面测，或把 JSON 写进文件用 `-d @req.json`。
10. **`sys.path` 依赖运行目录**：必须在 `python\` 下启动，否则 `.env` 与本地包（`config`/`agents`/...）都找不到。

---

## 5. 如果目标是"把它变成简历项目"

**核心原则：选一条主线做深，别把 4 个 Agent 都浅改一遍** —— 那样它仍然是个 demo。两条推荐主线：

**主线 A：可观测性与稳定性（改动量最小，面试最能讲清）**
- 补齐真实指标：把 `services/metrics.py` 接到 Prometheus `/metrics` 端点
- 做**可复现的降级验证**：注入故障让某个 Agent 必然失败（比如给个错的 base_url），验证系统仍返回 200 并说明降级策略
- 压延迟：缓存 + 提示词瘦身 + 把 Phase3 与 Phase2 合并并行，给出"优化前 15.8s → 优化后 X s"的前后对比

**主线 B：真实数据层 + 检索（工程完整度最强）**
- SQLAlchemy + Redis 特征 + 向量召回，全面替换 mock 数据
- 为召回做**离线评测**：构造 N 个用户，出 recall@k / 命中率，形成可复现的评测脚本

**叙述建议**：README 自带的"简历写法"里有一批未经实测的数字（CTR +15%、文案点击率 +23% 等），这些**不要照抄**——被追问"你怎么测的 A/B"会非常被动。你手上有真实可复现的延迟数据与降级链路，比那些数字更经得起问。

---

## 6. 日常启动

```bat
:: 双击即可（自动用绝对路径调用 python\.venv，不会串台）
D:\devlop\multi-agent-ecommerce-system\start-python.bat

:: 需要换端口（同时跑多个实例调试时）
start-python.bat 8011
```

脚本已实测：能在指定端口拉起服务，`/health` 返回 200。

> 建议顺手删掉仓库根目录那个空的 `.venv`，彻底消除 venv 歧义（需你确认后执行）：
> `cd /d D:\devlop\multi-agent-ecommerce-system && rmdir /s /q .venv`
