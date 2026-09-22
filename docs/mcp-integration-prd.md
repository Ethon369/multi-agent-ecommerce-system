# PRD + 技术选型：为推荐系统接入 MCP

| 项 | 内容 |
|---|---|
| 文档版本 | v1（**待评审，通过后才动代码**） |
| 日期 | 2026-09-20 |
| 范围 | 仅 Python 版（Java / Go 版不动） |
| 事实来源 | 本机实测 + MCP Python SDK v2 官方文档（非博客、非 README 宣传） |

---

## 1. 背景与目标

### 1.1 为什么要做这件事

三个具体问题，不是一个模糊的"想加 MCP"：

1. **能力无法被外部复用。** 当前四个 Agent 只能通过 HTTP REST（`POST /api/v1/recommend`，`main.py:73`）暴露。任何外部 LLM 应用（Claude Desktop、Cursor、其他 Agent）想用这套推荐能力，都必须自己写 HTTP 客户端并逆向理解 JSON 结构。MCP 提供标准化的工具发现与调用契约。

2. **项目"声称有 MCP，实际一行没有"。** `agents/inventory_agent.py:1-6` 的文件头写着"MCP协议同步WMS"，但：
   - `:30` `self.db: Any = None  # injected in Phase 2`
   - `:82-83` `if self.db: pass  # Phase 2: real DB query via MCP`
   
   这是个**空钩子**。README 把它描述成已完成能力，属于典型的"宣传与实现脱节"。接线它既补上功能，也修正了文档可信度问题。

3. **这是最能体现"二次开发"含金量的改动。** 它同时触及：协议层（MCP）、进程层（stdio 子进程生命周期）、数据层（SQLite）、以及**降级设计**（MCP 挂掉系统仍可用）。比"换模型、改提示词"这类 L1 改动有说服力得多。

### 1.2 目标

一句话：**把项目从"MCP 的宣称者"变成"MCP 的真实使用者"，并且双向都做。**

| 方向 | 内容 | 价值 |
|---|---|---|
| **B（服务端）** | 把推荐 / 指标能力封装为 MCP Server，任何 MCP Host 可直接调用 | 让项目"可被 AI 消费"，是 MCP 最主流的用法 |
| **A1（客户端）** | 库存 Agent 通过 MCP 客户端调用一个 SQLite 版 WMS Server，落地作者预留的 `self.db` 钩子 | 真正用掉作者的预留设计，且是**可降级**的（见决策 5） |

**为什么优先 B：** 服务端方向是"增值"（新增能力），客户端方向是"替换"（改已跑通的库存逻辑，有回归风险）。先用低风险方向把 MCP 基础设施（依赖、传输、生命周期、测试手法）立起来，再动核心链路。

### 1.3 非目标（明确不做，防止范围蔓延）

- 不做 Milvus / 向量召回 —— 与 MCP 无关，属另一条主线
- 不做 Prometheus `/metrics` 端点 —— 属"可观测性"主线（见 `docs/extension-guide.md` §5）
- 不改 Java / Go 版 —— 三语言代码各自独立，改 Python 不会同步
- 不做 MCP 的 auth / OAuth —— stdio 本地场景用不到，接入远程 HTTP 时再谈
- 不做 WebSocket 传输 —— **mcp v2 已彻底移除该传输**（非规范内容）
- 不实现 MCP 的 Tasks 扩展（SEP-2663）—— v2 SDK 尚未实现

---

## 2. 交付物清单

| # | 交付物 | 路径 | 类型 |
|---|---|---|---|
| D1 | 推荐能力 MCP Server | `python/mcp_servers/recommend_server.py` | 新增 |
| D2 | SQLite WMS 库存 MCP Server | `python/mcp_servers/wms_server.py` | 新增 |
| D3 | WMS 建表 + 种子数据脚本 | `python/mcp_servers/init_wms_db.py` | 新增 |
| D4 | MCP 客户端封装（供 InventoryAgent 调用） | `python/services/mcp_client.py` | 新增 |
| D5 | InventoryAgent 接线（落地 `self.db` 钩子） | `python/agents/inventory_agent.py` | **修改** |
| D6 | 配置项（MCP 开关、DB 路径、超时） | `python/config/settings.py` + `.env.example` | **修改** |
| D7 | MCP 生命周期管理 | `python/main.py` | **修改** |
| D8 | 冒烟 + 降级测试 | `python/tests/test_mcp_wms.py` | 新增 |
| D9 | 使用文档（怎么跑、怎么验证、怎么扩） | `docs/mcp-integration.md` | 新增 |
| D10 | 依赖声明 | `python/requirements.txt` | **修改** |

只有 3 个文件是"修改既有代码"（D5/D6/D7），其余全是新增。这是有意的：**新增优于修改**，降低破坏已跑通环境的风险。

---

## 3. 架构设计

### 3.1 拓扑

```
        ┌──────────────────────────────────────┐
        │  外部 MCP Host                        │
        │  (Claude Desktop / Cursor / IDE)      │
        └────────────────┬─────────────────────┘
                         │ stdio（MCP 协议）
                         ▼
        ┌──────────────────────────────────────┐
        │  D1  recommend_server                │
        │  tools:                              │
        │   - recommend_products               │
        │   - get_metrics                      │
        └────────────────┬─────────────────────┘
                         │ 直接 import 复用业务代码
                         ▼
  ┌──────────────────────────────────────────────────────────┐
  │  FastAPI（main.py）—— 原有 REST 接口完全不变              │
  │    └─ SupervisorOrchestrator                             │
  │         └─ InventoryAgent                                │
  │              └─ D4 mcp_client ────── stdio ────┐          │
  └────────────────────────────────────────────────┼──────────┘
                                                   ▼
                                 ┌──────────────────────────┐
                                 │  D2  wms_server          │
                                 │  tools:                  │
                                 │   - query_stock          │
                                 │   - batch_query_stock    │
                                 │   - list_low_stock       │
                                 │   - upsert_stock         │
                                 │  resource: wms://stock/… │
                                 │  SQLite: wms.db          │
                                 └──────────────────────────┘
```

**关键点：两个 Server 都不重复实现业务逻辑。** D1 直接 import 复用 `SupervisorOrchestrator` / `MetricsCollector`（MCP Server 只是换个协议壳）；D2 是唯一新增的业务逻辑（真实的库存表）。

### 3.2 一次"带 MCP 的推荐请求"的数据流

```
POST /api/v1/recommend
  └─ supervisor.recommend()
       ├─ Phase 1（并行）：UserProfileAgent ∥ ProductRecAgent
       ├─ Phase 2（并行）：
       │    ├─ ProductRecAgent（重排）
       │    └─ InventoryAgent
       │         └─ _check_stock()  ←── 唯一改动点（inventory_agent.py:81）
       │              ├─ MCP 开：mcp_client.batch_query_stock([...]) ──stdio──> wms_server ──> SQLite
       │              └─ MCP 关/失败：返回 fallback_stock（即 Product.stock）  ←── 原有行为
       ├─ 库存过滤 + TopN 截断
       └─ Phase 3（串行）：MarketingCopyAgent
```

改动被压缩到**一个方法**里。`_check_stock()` 已经是"有 db 走 db、没有走 fallback"的结构，我们只是把 `if self.db: pass` 填上。

### 3.3 关键设计决策（含被否决方案）

#### 决策 1：用 mcp v2（2.2.0），不用 v1 的 `FastMCP`

这是本次最重要的选型，也是**最容易被踩坑的地方**：v2 是一次大重构，导包路径直接变了，而且旧路径是**删除而非弃用**。

| 对比项 | v1（`FastMCP`） | v2（`MCPServer`） |
|---|---|---|
| 导包 | `from mcp.server.fastmcp import FastMCP` | **`from mcp.server import MCPServer`** |
| 实例化 | `FastMCP("Demo")` | `MCPServer("Demo")` |
| 子模块 | `mcp.server.fastmcp.*` | `mcp.server.mcpserver.*` |
| 上下文 | `ctx.fastmcp` / `get_context()` | `ctx.mcp_server` / 显式声明 `ctx: Context` 参数 |
| 异常基类 | `FastMCPError` | `MCPServerError` |
| 传输参数位置 | 构造函数上 | **全部移到 `run()`**（`MCPServer("x", port=9000)` 会 `TypeError`） |
| 类型字段命名 | camelCase | **snake_case**（`result.is_error`、`tool.input_schema`） |
| 客户端 | 三层嵌套（transport → ClientSession → `initialize()`） | 一等 `Client` 对象 |
| `pip install mcp` 装到 | 需显式 `mcp<2` | **2.x（默认）** |
| 维护状态 | 维护模式，仅关键修复 | 当前稳定线 |

**选 v2 的理由（按权重排序）：**

1. **`pip install mcp` 默认就是 2.x。** 想留在 v1 必须主动写上限 `mcp>=1.28,<2`。选 v2 是顺流，选 v1 是逆流。
2. **v2 同时服务两个协议纪元**（2025-11-25 与 2026-07-28）。这意味着 Host 侧（Claude Desktop、Cursor 等）兼容性**不受 SDK 版本影响** —— 这消除了"用新版会不会连不上"的顾虑。
3. **v2 自带 in-memory 测试能力**：`async with Client(mcp)` 直接把 Server 对象当传输，不启子进程、不占端口、不走 JSON-RPC。这让 D8 的测试能真正跑起来（v1 需要 `create_connected_server_and_client_session()` 辅助函数，且已被移除）。
4. **辅助函数签名更宽松**，且在 v2 里同步函数（`def`）会自动下放到工作线程执行，不阻塞事件循环 —— 这一点与决策 4（同步 SQLAlchemy）天然契合。

**代价（必须承认）：**

- 网上绝大多数 MCP 教程、博客、以及模型训练数据里的例子**都是 v1 的 `FastMCP`**。照抄会直接 `ImportError`。**对策：只以官方 v2 文档为准，不抄博客**（附录 A 已把关键 API 抄好）。
- 生态工具链滞后：部分第三方 SDK 仍锁 `mcp<2`（见决策 2）。

**回退方案：** 若 v2 遇到无法绕开的阻塞，改为 `mcp>=1.28,<2` + `FastMCP`，代码结构不变（仅换导包与 `run()` 参数位置），成本约 1 小时。

#### 决策 2：**不使用** `langchain-mcp-adapters`

已从 PyPI 元数据核实（非推测）：

```
langchain-mcp-adapters 0.3.2
  requires_dist:
    mcp<2.0.0,>=1.24.0          ← 强制降级 mcp 到 v1
    langchain-core<2.0.0,>=1.3.3
    typing-extensions>=4.14.0
```

**否决理由：**

1. 它会把 `mcp` 锁死在 `<2.0.0`，与决策 1 直接冲突。
2. **本项目用不上它。** 它的用途是把 MCP 工具转成 LangChain Tool 供 **LangChain Agent** 调用。而本项目的四个 Agent 是自定义的 `BaseAgent` 子类（`agents/base_agent.py`），不是 LangChain Agent；编排用的是手写 `asyncio.gather` 与 LangGraph `StateGraph`（`orchestrator/graph.py`），两者都不消费 LangChain Tool。
3. 引入它会多一层抽象与一个版本枷锁，换来的能力当前为 0。

**未来触发条件：** 若某天要让 LangGraph 节点直接以"工具调用"形式使用 MCP Server，再单独评估 —— 那时的正确做法是**新建一个隔离 venv** 走 v1 路线，而不是把主环境降级。

#### 决策 3：stdio 传输，不用 streamable-http

| 维度 | stdio（选用） | streamable-http |
|---|---|---|
| Host 配置 | 一行 `command` + `args` | 需先起服务、配 URL |
| 端口冲突 | 不存在 | 需管理端口（本机已有 8000/8011 等占用史） |
| 跨机共享 | 不能 | 能 |
| 调试 | 不便（无 HTTP 可 curl） | 可用 curl / Inspector |
| 生命周期 | 绑定调用方进程 | 独立 |

**选 stdio 的理由：** 本地二次开发场景，零网络配置、零端口冲突是第一优先级；用户此前已在端口与进程管理上踩过坑（见扩展指南"坑"章节）。

**何时该换 HTTP（判据）：** 需要跨机调用、需要多客户端共享同一个 Server 实例、或需要独立部署 Server 时。`main.py` 已在用 FastAPI，v2 支持把 MCP 的 ASGI app **挂载到现有 FastAPI 应用**（`mount_path` 在 v2 已移除，改为挂载 ASGI app），可作为后续演进方向。

#### 决策 4：SQLite 作为 WMS 存储

**选它的理由：** 零运维、单文件、可作为种子数据提交进仓库供演示；且 `SQLAlchemy 2.0.54` **已经在依赖里**（`requirements.txt`），无需新增。

**附加决策 4b：为什么不用现成的 SQLite MCP Server（已核实，非推测）**

第一直觉是"既然生态里有现成 SQLite MCP Server，何必自己写"。核实后否决，三条理由：

1. **官方那个已归档。** `mcp-server-sqlite`（PyPI，Python）随 SQLite / PostgreSQL / Redis / GitHub 等一批参考服务器，于 2025-05-29 被移入 `servers-archived` 仓库。官方 `modelcontextprotocol/servers` 现仅维护 7 个（Everything / Fetch / Filesystem / Git / Memory / Sequential Thinking / Time）。归档意味着不再有更新与安全补丁。
2. **归档的 SQLite server 存在 SQL 注入问题（CWE-89），且无只读模式**，官方不会修。任何可写的 `.db` 文件它都会照单执行 `write_query` / `INSERT` / `UPDATE` / `DELETE`。拿它接真实库存数据，是主动引入一个已知漏洞。同类问题也出现在已弃用的官方 Postgres MCP Server 上（Datadog Security Labs 有完整案例分析）。
3. **更关键的是语义不匹配。** 它的工具形态是 `read_query(sql)` / `write_query(sql)` —— **通用 SQL 入口**，意味着要由模型自己拼 SQL。而库存查询必须是**确定性的**（给定 `product_id` 就必须返回那条库存），让模型写 SQL 既引入不确定性，又扩大了攻击面。

**结论：** 本节自己写 `wms_server`（D2）**不是重复造轮子**，而是因为现成方案在维护状态、安全性、语义匹配三个维度上都不合格。这条本身就是值得写进面试话术的判断（"我评估过现成方案，为什么不采用"）。

> 若将来确实需要"通用 SQL 查询"能力（例如给运营做即席分析），更合理的选择是社区里仍在维护的方案，而不是已归档的官方实现 —— 候选：Bytebase **DBHub**（多数据库、活跃维护）、`jparkerweb/mcp-sqlite`（语义化 CRUD + 参数绑定，2026-04 修了 CWE-89）、`sqlite-explorer-fastmcp`（只读设计）。但这属于另一个需求，不在本次范围内。

**一个漂亮的契合点：** v2 中同步 `def` 工具函数会自动运行在工作线程（不阻塞事件循环）。因此这里**可以放心使用同步 SQLAlchemy 引擎**，不需要 `aiosqlite`、不需要 `run_in_threadpool` 手工包装。若用 v1 或写成 `async def`，则必须引入 `aiosqlite` 并处理并发。这反过来又强化了决策 1。

**表结构：**

```sql
CREATE TABLE IF NOT EXISTS wms_stock (
    product_id          TEXT    PRIMARY KEY,
    stock               INTEGER NOT NULL DEFAULT 0,
    safety_threshold    INTEGER NOT NULL DEFAULT 50,
    low_stock_threshold INTEGER NOT NULL DEFAULT 100,
    updated_at          TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wms_stock_stock ON wms_stock(stock);
```

**阈值来源：** 沿用 `agents/inventory_agent.py:16-18` 已有的 `SAFETY_STOCK_THRESHOLD=50` / `LOW_STOCK_THRESHOLD=100`，不另造一套。**但把它们存进表**（而非写死在代码里）—— 这样不同商品可以有不同阈值，且改阈值不用发版。这是相对原代码的一个实质改进。

**种子数据：** 由 `MOCK_PRODUCTS`（`agents/product_rec_agent.py:41-57`，15 条）自动生成，并**故意制造差异**：部分商品的 WMS 库存与 `Product.stock` 故意不一致、部分为 0。目的是让验收项 A5 能证明"真的走了 MCP"，而不是"看起来一样所以大概是通的"。

#### 决策 5（最重要）：MCP 必须可降级，这是硬性验收标准

**规则：MCP Server 起不来、超时、返回异常 → `InventoryAgent` 必须回落到 `Product.stock`（即 `_check_stock` 原有的 fallback 行为），推荐接口仍返回 HTTP 200。**

**理由：**

1. 作者原设计里 `BaseAgent` 已内建降级（`_fallback()` + tenacity 重试 + `asyncio.wait_for` 超时）。MCP 是外挂依赖，**不该成为新的单点故障**。
2. `/api/v1/recommend` 是主业务链路，为了一个库存查询把整条链路拖垮，工程上不可接受。
3. 这条直接构成一个有说服力的面试素材：**"我给新增的 MCP 依赖设了降级路径，并用故障注入验证过"** —— 比"我接了 MCP"高一个层次。

**实现要求：** 降级时必须在返回的 `data` 里标注 `"source": "fallback"`（成功时标 `"source": "mcp"`），使降级**可观测**，否则"降级"会变成静默的错误掩盖。

**超时设定：** `ECOM_MCP_WMS_TIMEOUT=3.0` 秒。判据：库存查询是毫秒级操作，3 秒不给响应说明进程已异常，继续等待只会拖慢主链路。这个值远小于 Agent 自身超时（`agent_timeout_inventory=5.0`），确保 MCP 先超时、Agent 还有余量走 fallback。

---

## 4. 工具契约（接口设计）

### 4.1 D1 `recommend_server`

```python
recommend_products(
    user_id: str,
    scene: str = "homepage",
    num_items: int = 10,
    context: dict | None = None,
) -> dict
```
- 说明：完整四 Agent 编排。**实测延迟 15.8–16.6 秒**（真实 LLM），不是 README 宣称的 2 秒。
- 返回：`{request_id, products[], marketing_copies[], total_latency_ms}`
- **设计注意：** 显式构造扁平返回结构，不直接返回 `RecommendationResponse.model_dump()`。
  **原始理由**是绕开 pydantic 的子类字段截断（当时 `agent_results` 声明为 `dict[str, AgentResult]`）—— 该 bug 已于 2026-09-21 修复。
  **保留扁平结构的现行理由**：信息都在顶层，对 Host 侧模型更友好（不用猜嵌套），也顺带挡掉 `latency`/`confidence` 这类运维细节，省 token。
- 工具描述里必须写明"约 16 秒"，引导 Host 侧模型正确预期，避免被判定为超时。

```python
get_metrics() -> dict                          # 复用 main.py:126-132 的逻辑
```

**全部是只读工具**，避免 MCP Host 侧的模型误触发副作用。
> 原设计里还有 `get_experiments` 与 `record_experiment_outcome`（唯一写操作）两个工具，
> 它们随那个没接线的 A/B 引擎一并删除 —— 项目没有真实流量，A/B 测不出东西，
> 前后对比改用 `python/eval/` 的离线评测集。

### 4.2 D2 `wms_server`

```python
query_stock(product_id: str) -> dict
# → {product_id, stock, safety_threshold, low_stock_threshold, updated_at}

batch_query_stock(product_ids: list[str]) -> dict[str, int]
# → {"P001": 120, "P003": 0, ...}   供 InventoryAgent 一次批量取，避免 N 次往返

list_low_stock(threshold: int = 100) -> list[dict]
# → 库存低于阈值的商品清单（含 level: critical|warning）

upsert_stock(product_id: str, stock: int) -> dict
# → 仅用于演示/测试造数据
```

**资源（resource）：** `wms://stock/{product_id}` —— 演示 MCP 的第二种能力类型（工具之外），让 Host 侧可以"读"而不只是"调用"。URI 模板在 v2 中已是真正的 RFC 6570 实现。

**为什么要有 `batch_query_stock`：** 一次推荐要查最多 30 个商品（Phase 1 召回 `num_items * 2`）。逐个 `query_stock` 意味着 30 次 stdio 往返。批量接口把这个降为 1 次。这是**由实际调用模式倒推出的接口设计**，不是凑数。

---

## 5. 配置与开关

`.env` 新增：

```ini
# MCP（默认全部关闭，不影响现有启动）
ECOM_MCP_ENABLED=false              # 是否启用 recommend_server
ECOM_MCP_WMS_ENABLED=false          # 是否启用 WMS MCP 客户端接线
ECOM_MCP_WMS_DB_PATH=./wms.db       # 相对于 python/ 目录
ECOM_MCP_WMS_TIMEOUT=3.0            # 秒，必须 < agent_timeout_inventory(5.0)
```

`config/settings.py` 在 `Database` 段落后新增对应字段（`env_prefix="ECOM_"` 已配好，无需额外处理）。

**为什么默认 `false`：** 现环境是"装好依赖、已跑通全链路"的状态。默认开启会让 MCP 成为启动前置条件，一旦子进程问题就会表现成"服务起不来"。**默认关闭 + 显式启用**，保证零破坏。

---

## 6. 生命周期管理（最容易翻车的一节）

### 问题 1：`--reload` 会杀掉 stdio 子进程

`main.py:152` 是 `uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)`。`reload=True` 会额外起一个 reloader 父进程，代码变更时**杀掉整个子进程树**。若 MCP 连接是导入期建立的常驻长连接，它就是第一个牺牲品 —— 而客户端仍持有那个死连接的引用 → 表现为"卡住"或 `BrokenPipe`，且**日志里看不出原因**。

**方案（按优先级）：**

1. **惰性连接 + 每次调用建立短连接。** `mcp_client` 不持有常驻连接，每次 `batch_query_stock` 走一次 `async with Client(StdioServerParameters(...))`。代价是每次调用多一次进程启动开销（约 100–300 ms），换来的是**完全的健壮性**。对"每请求调用一次"的用量，这是正确取舍。
2. 若将来改为常驻连接（用量上升后再优化），必须在 FastAPI `lifespan` 里管理，且**在 `--reload` 模式下不可依赖它**。`main.py:44-50` 已有 `lifespan`，扩展点现成。

**本次采用方案 1。**

### 问题 2：Windows 子进程必须用绝对路径

启动 MCP Server 子进程时：

```python
StdioServerParameters(
    command=sys.executable,          # 必须是绝对路径，不能写 "python"
    args=[str(SERVER_PATH)],
    cwd=str(PYTHON_DIR),             # 必须是 python/ 目录
    env={...},
)
```

两个必须：

- **`command` 用 `sys.executable`。** 直接写 `"python"` 会命中 PATH 上的任意解释器 —— 这台机器上有 **11 个解释器、其中 6 个叫同样名字的 `.venv`**（已实测），必然重演"包明明装了却 import 不到"的 venv 串台问题。当前进程的 `sys.executable` 天然就是正确的那个。
- **`cwd` 必须是 `python/` 目录。** 项目的 `sys.path` 依赖运行目录（`main.py:17` 的 `sys.path.insert`，以及 `config` / `agents` 等本地包），换目录启动会直接 `ModuleNotFoundError`。这是已实测的坑。

---

## 7. 测试与验收

| # | 验收项 | 方法 | 通过标准 |
|---|---|---|---|
| A1 | Server 可被发现 | `mcp dev python/mcp_servers/wms_server.py` | Inspector 中列出全部工具，且 tool schema 正确 |
| A2 | 工具逻辑正确（免子进程） | `async with Client(wms_mcp) as c:` 直接连 Server 对象 | 返回的 stock 与 SQLite 实际值一致 |
| A3 | 端到端 | 启动 FastAPI，`POST /api/v1/recommend` | HTTP 200；日志出现 MCP tool call |
| A4 | **降级验证（最重要）** | 将 `ECOM_MCP_WMS_DB_PATH` 指向不存在的路径 / 强杀 Server 子进程 | 推荐**仍 200**；`inventory` 的 `success=True` 走 fallback；`data.source == "fallback"` |
| A5 | 契约验证（证明真接了） | 断言 WMS 库存 ≠ `Product.stock` 时以 WMS 为准 | 结果随 WMS 变化；`data.source == "mcp"` |
| A6 | 回归 | `probe_recommend.py`（归档于 `D:\devlop\_archive\probes_20260920`） | 四个 Agent 全 `success=True`，延迟与基线（15.8–16.6s）同量级 |
| A7 | 关闭开关时零影响 | `ECOM_MCP_WMS_ENABLED=false` 启动 | 行为与改动前完全一致 |

**A4 是本方案的灵魂。** 只做 A3 只能证明"能用"，A4 才能证明"挂掉也不怕" —— 后者才是生产可用的判据。

---

## 8. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| **mcp v2 是新 API，网上教程全是 v1 的 `FastMCP`** | 照抄即 `ImportError`，排查耗时 | 只以官方 v2 文档为准（附录 A 已提炼）；不抄博客 |
| **Windows 安装 mcp 会拉入 pywin32 / httpx2 / sse-starlette / opentelemetry-api** | 依赖体积增大，可能与现有版本冲突 | 已核实：`starlette>=0.48` 满足（项目 1.6.0）、`pydantic>=2.12` 满足（项目 2.13.5）；**先在副本 venv 验证安装，确认无冲突再动主 venv** |
| stdio 子进程与 `--reload` 冲突 | 子进程被杀，表现为莫名卡住 | 采用惰性短连接（§6 方案 1） |
| MCP 往返叠加 LLM 延迟 | 用户感知更慢 | MCP 只用于库存查询（毫秒级），不参与 LLM 链路；主链路三段串行仍是主要成本 |
| 改动 D5 触及"已跑通"的库存逻辑 | 回归风险 | 默认开关关闭 + A6 回归 + A7 零影响验证 |
| MCP Server 子进程里 `get_settings()` 读不到 `.env` | LLM 为空的 Agent 静默降级 | 子进程 `cwd` 固定为 `python/`，且显式传 `env` |

---

## 9. 里程碑（每阶段可独立验证，不通过不进下一阶段）

| 阶段 | 内容 | 完成标志 |
|---|---|---|
| **P0** | 本文档评审 | 你确认选型与范围（尤其 §10 三个决策点） |
| **P1** | 依赖 + 骨架：装 `mcp`、写 D2 + D3 | A1 通过（`mcp dev` 能列出工具） |
| **P2** | 客户端接线：D4 + D5 + D6 | A2 / A3 通过 |
| **P3** | 服务端方向：D1 | 外部 Host 能调用 `recommend_products` |
| **P4** | 降级与收尾：A4 / A5 / A6 / A7 + D9 文档 | 全部验收项通过 |

---

## 10. 待你决策的三个点

### 决策点 1：mcp 版本路线

- **方案 A（推荐）：mcp v2 (`MCPServer`)** —— 顺流、服务双协议纪元、自带 in-memory 测试。代价：查不到现成教程。
- **方案 B：mcp v1 (`FastMCP`)** —— 教程丰富、兼容 `langchain-mcp-adapters`。代价：维护模式、需手工加 `mcp<2` 上限、测试麻烦。

> 我的建议是 **A**。理由已在决策 1 列出，核心是"`pip install mcp` 默认就是 v2，而 v2 不影响 Host 兼容性"。

### 决策点 2：是否保留 `recommend_products` 这个慢工具

它要 16 秒。保留可以完整展示"MCP 封装复杂多 Agent 编排"，但 Host 侧体验差（多数 Host 有 30–60s 工具超时，能过但很慢）。
- **保留**：完整度优先
- **改为异步任务模式**（先返回 `request_id`，再查结果）：体验好，但工作量翻倍
- **移除**：只保留 `get_metrics` 这类快工具

> 建议：**先保留**，在工具描述里明确标注耗时。异步模式作为后续演进。

### 决策点 3：是否顺带清理根目录的空 `.venv`

仓库根目录有个空 `.venv`（仅 2 个包），与 `python/.venv`（160 个包）同名，是 venv 串台问题的根源之一。

```
cd /d D:\devlop\multi-agent-ecommerce-system && rmdir /s /q .venv
```

> 需你明确同意后才执行。这是我唯一想动的"既有文件"之外的东西，且是删除操作，故单独列出。

---

## 附录 A：mcp v1 → v2 API 速查（写代码时照这个，不要照博客）

```python
# 服务端
from mcp.server import MCPServer          # v1: from mcp.server.fastmcp import FastMCP
mcp = MCPServer("ecom-wms")

@mcp.tool()
def query_stock(product_id: str) -> dict:
    """查询单个商品库存。"""
    ...

@mcp.resource("wms://stock/{product_id}")
def stock_resource(product_id: str) -> str:
    ...

if __name__ == "__main__":
    mcp.run(transport="stdio")            # v2：host/port 等传输参数都在 run() 上

# 客户端（同包，无需 adapter）
from mcp import Client
from mcp.client.stdio import StdioServerParameters

params = StdioServerParameters(command=sys.executable, args=[str(SERVER)], cwd=str(PYTHON_DIR))
async with Client(params) as client:
    tools = await client.list_tools()
    result = await client.call_tool("batch_query_stock", {"product_ids": ["P001"]})

# 测试：直接把 Server 对象当传输，零子进程零端口
async with Client(wms_mcp) as client:
    result = await client.call_tool("query_stock", {"product_id": "P001"})
```

**必须记住的差异：**

1. `mcp.server.fastmcp.*` → `mcp.server.mcpserver.*`（旧路径**已删除**，非弃用）
2. 传输参数（`host`/`port`/`stateless_http`/`transport_security`）→ 全部在 `run()` 上
3. 类型字段 camelCase → **snake_case**（`is_error`、`input_schema`、`next_cursor`）
4. `McpError` → `MCPError`；`FastMCPError` → `MCPServerError`
5. `get_context()` 已移除 → 处理器里显式声明 `ctx: Context` 参数
6. 同步 `def` 处理器自动跑在工作线程；`async def` 保持原样
7. 工具内抛 `MCPError` 是**协议错误**（模型看不到）；抛其他异常会变成 `is_error=True` 结果，但**只有 `ToolError` 的消息会传给模型**
8. span 传输层已移除 WebSocket
9. `mcp dev` / `mcp install` 会把环境钉在你安装的 SDK 版本上
10. `mcp.types` 仍是永久别名（底层已拆到独立的 `mcp-types` 包）

**官方文档：** <https://py.sdk.modelcontextprotocol.io/>（v1 文档在 `/v1/`）

---

## 附录 B：本文档的实测依据

| 结论 | 验证方式 | 结果 |
|---|---|---|
| `mcp` 最新版为 2.2.0 | PyPI JSON 元数据 | 2.2.0，`requires_python >=3.10` |
| `mcp` 在 Python 3.14 可解析 | `pip install --dry-run` | 命中 `mcp-2.2.0-py3-none-any.whl` |
| adapter 与 v2 不兼容 | PyPI JSON 元数据 | `mcp<2.0.0,>=1.24.0` |
| 项目 langchain-core 满足 adapter 要求 | 本机元数据 | 1.6.3（要求 `>=1.3.3,<2.0.0`） |
| 项目 starlette/pydantic 满足 mcp v2 要求 | 本机元数据 | starlette 1.6.0（要求 ≥0.48）、pydantic 2.13.5（要求 ≥2.12） |
| v2 导包路径为 `mcp.server.MCPServer` | 官方 v2 文档 | `FastMCP` 已重命名，旧路径删除 |
| 当前 venv 未装 mcp | 本机元数据查询 | `mcp` / `fastmcp` / `langchain-mcp-adapters` 均 MISSING |
| `agent_results` 子类字段被截断 | 前序会话实测响应体 | 仅剩基类 6 字段（**已于 2026-09-21 修复**，见 progress.md） |
| 全链路真实延迟 | 前序会话实测 | 15.8–16.6 s（profile 7.97 + rerank 4.13 + copy 3.74） |
| 官方参考服务器仅剩 7 个 | 官方仓库 README | Everything / Fetch / Filesystem / Git / Memory / Sequential Thinking / Time |
| SQLite / PostgreSQL / Redis 等官方 Server 已归档 | 官方仓库 README「Archived」段 | 移入 `servers-archived`（2025-05-29） |
| 官方声明参考服务器非生产可用 | 官方仓库 README 警告段 | "reference implementations… not as production-ready solutions" |
| 归档 SQLite server 存在 SQL 注入 | 第三方安全评测（CWE-89）、Datadog Security Labs 对官方 Postgres server 的案例分析 | 无只读模式，无护栏 |
| 本机 `npx` 可用 | `npx --version` | 11.19.0（node v22.22.2） |
| 本机 `uvx` / `uv` **未安装** | `command -v uv` | 官方 Python 类 Server 的默认启动方式在本机不可用 |
| 本机 `docker` 可用 | `docker --version` | 29.7.2（可走 Docker MCP Toolkit） |

---

## 附录 C：可选增强 —— 引入现成 MCP Server（零代码）

除"写自己的 Server"外，另一种用法是**装现成的 Server 并注册到 MCP Host**（Claude Desktop / Cursor / 本机 WorkBuddy 的 `~/.workbuddy/mcp.json`）。这条路径不需动本项目任何代码。

**筛选原则：只保留能解决真实问题的，不为"显得用了 MCP"而加。** 加一个用不上的 Server 只会增加攻击面与启动开销。

| 候选 | 启动方式 | 对本项目的实际用途 | 判断 |
|---|---|---|---|
| `server-time` | Python（本机需先装 uv，或 `pip install mcp-server-time`） | 营销文案里的"限时/当季/节日"话术目前靠 LLM 猜时间；接真实时间可让话术有依据 | **建议** |
| `server-fetch` | `npx -y @modelcontextprotocol/server-fetch` | 营销 Agent 抓取商品页/竞品信息提炼卖点 | **建议（需评估外网与合规）** |
| `server-memory` | `npx -y @modelcontextprotocol/server-memory` | 跨会话用户长期偏好（知识图谱）。注意与本项目**已接线的 Redis 特征层**（`services/feature_store.py`）职责重叠：那层管的是"近期行为窗口"，知识图谱管的是"长期偏好"，可以并存但别重复建设 | 可选 |
| `server-everything` | 官方参考 | 学习 MCP 的工具/资源/提示词三类能力全貌 | 学习用 |
| `server-sequentialthinking` | 官方参考 | 复杂推理规划 | **不建议**：本项目已 16 s 延迟，再加推理链更慢；四 Agent 是确定性编排 |
| `server-filesystem` | 官方参考 | 文件读写 | **不建议**：项目只有 `image_url` 字段，无真实图片文件 |
| `server-git` | 官方参考 | Git 操作 | **不建议**：运行时无关 |

**本机环境注意（已实测）：**

- `npx` **可用**（11.19.0）；`uvx` / `uv` **未安装** → 官方 Python 类 Server 的默认 `uvx mcp-server-xxx` 在本机跑不起来，需改用 `pip install mcp-server-xxx` + `python -m mcp_server_xxx`，或先装 uv。
- **Windows 上 `npx` 必须用 `cmd /c` 包裹**，这是官方 README 明确要求的：
  ```json
  { "mcpServers": { "fetch": { "command": "cmd", "args": ["/c", "npx", "-y", "@modelcontextprotocol/server-fetch"] } } }
  ```
- 浏览更多已发布 Server 请用官方注册表 <https://registry.modelcontextprotocol.io/>（官方仓库自身只放参考实现，不是 Server 列表）。
