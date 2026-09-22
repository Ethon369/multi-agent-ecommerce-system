# CLAUDE.md — 给 AI Agent 的项目说明

> 这个文件是给 AI 助手看的（Claude Code / Cursor / 其他 agent）。
> 目的：**让你不用重新踩一遍已经踩过的坑**。
> 人类读者请看 [README.md](README.md) 和 [docs/progress.md](docs/progress.md)。

---

## 一、开工先读这三份（按顺序）

| 文件 | 读它是为了 |
|---|---|
| [docs/progress.md](docs/progress.md) | **当前做到哪了** —— 里程碑状态、MCP 交付物进度、实测数字、已知问题、下一步 |
| [docs/harness-mcp-walkthrough.md](docs/harness-mcp-walkthrough.md) | harness 与 MCP 在本项目里**具体是哪几行代码** |
| [docs/mcp-integration-prd.md](docs/mcp-integration-prd.md) | MCP 部分的原始设计（512 行，含被否决方案的论证） |

**改代码前还要看**：`git log` —— 最近 8 个 commit 的 message 里写着**每个设计决策的理由和踩过的坑**，比代码注释更完整。

---

## 二、怎么跑（命令必须照抄，别"优化"）

> ⚠️ **2026-09-22：目录已从 `python/` 重命名为 `backend/`。**
> 本文档、`README.md`、`docker-compose.yml`、`start-python.bat` 里原先
> 写的都是 `python/`。compose 的 `build.context` 和启动脚本因此**直接跑不起来**
> （compose 在构建第一步就失败，bat 卡在 `cd /d "%~dp0python"`）。
> 那一批已修；`docs/` 下更早的阶段文档里可能仍有残留 —— 见到 `python/`
> 一律按 `backend/` 读。

### 解释器：只能用绝对路径

```bash
# ✓ 正确
backend/.venv/Scripts/python.exe -m pytest tests/ -q

# ✗ 错误 —— 会命中根目录那个【空的假 venv】
.venv/Scripts/python.exe -m pytest
```

**这台机器上有 11 个 Python 解释器，其中 6 个目录名叫 `.venv`。**
根目录的 `.venv` 是废的（site-packages 是空的），真正的在 `backend/.venv`。
用错的表现是 `ModuleNotFoundError: No module named 'xxx'`，而你以为装了。

### 工作目录：必须在 `backend/`

```bash
cd backend && ./.venv/Scripts/python.exe -m pytest tests/ -q
```

原因：`config/settings.py` 用的是**相对路径** `env_file=".env"`，
`main.py` 也有 `sys.path.insert(...)`。cwd 不对 ⇒ **静默读不到配置**（不报错，直接用默认值）。

### 在别处跑脚本：要设 PYTHONPATH

```bash
cd backend && PYTHONPATH="D:/devlop/multi-agent-ecommerce-system/backend" \
  ./.venv/Scripts/python.exe /d/tmp/some_script.py
```

Python 把**脚本所在目录**加进 `sys.path`，不是 cwd。

### 起服务

```bash
cd backend && ./.venv/Scripts/python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000 --no-access-log
```

**调试和演示时加 `--no-access-log`。**

**关于 `--reload`**（2026-09-22 变更）：`main.py` 的 `__main__` 块原先硬编码
`reload=True`，现在改成读 `ECOM_DEV_RELOAD`，**默认 false**。原因是它和 MCP 冲突
（见陷阱 5）。要热重载就显式开：
```bash
ECOM_DEV_RELOAD=true ./.venv/Scripts/python.exe main.py
# 或者：start-python.bat 8000 reload
```

### 初始化 MCP 的库存数据库

```bash
cd backend && ./.venv/Scripts/python.exe mcp_servers/init_wms_db.py --reset
```

### 跑测试 + 覆盖率（和 CI 完全一致的两条命令）

```bash
cd backend
./.venv/Scripts/python.exe -m compileall -q .          # 语法检查，比等测试报 ImportError 快
./.venv/Scripts/python.exe -m pytest --cov --cov-report=term-missing
```

覆盖率门槛（`fail_under = 83`，实测 85.42%）在 `pyproject.toml` 里 ——
低于门槛时 pytest 会以非零码退出，不需要额外判断。

**这套测试必须离线跑绿**（不起服务、不调 LLM、不连 Redis）。CI 里刻意
**不提供** `ECOM_LLM_API_KEY` 就是为了守这条 —— 见陷阱 11。

---

## 三、十一个陷阱（都是实际踩过的）

### 1. 假的根目录 `.venv`
见上文。**永远用 `backend/.venv/Scripts/python.exe` 的绝对/相对完整路径。**

### 2. Git Bash 管道的编码问题
```bash
# ✗ 中文会变成乱码（管道按 GBK 解码 UTF-8）
curl ... | python -c "import json,sys; print(json.load(sys.stdin))"

# ✓ 落文件，再显式用 UTF-8 读
curl ... -o /d/tmp/x.json
python -c "
import json,io,sys
sys.stdout=io.TextIOWrapper(sys.stdout.buffer,encoding='utf-8')
print(json.load(open('D:/tmp/x.json',encoding='utf-8')))
"
```
注意：Python 在这个 shell 里要用 **Windows 路径**（`D:/tmp/x.json`），`/d/tmp/x.json` 它不认。

### 3. 测试隔离：两个全局状态必须清

`tests/conftest.py` 里有两个 **autouse** 装置，**不要删**：

- `_isolate_agent_runtime` —— `AgentRuntime` 是进程级单例，熔断状态会跨测试累积
- `captured` 装置里的 `clear_contextvars()` —— `structlog` 的 `bind_contextvars` 是**永久性**的

不同步清的表现：**随机某几条测试失败，且失败信息里显示的是别的测试留下的值**。

### 4. 不需要 `pytest-asyncio`
`anyio` 自带 pytest 插件（`pytest --version` 的 plugins 列表里能看到）。
异步测试用 `@pytest.mark.anyio`，`anyio_backend` fixture 已在 conftest 里钉死为 asyncio。

### 5. `uvicorn --reload` 会杀掉 MCP 的 stdio 子进程
reloader 重启时会杀掉整个子进程树，而 **import 期建立的 MCP 长连接会先死**，
症状是**静默挂起**或 `BrokenPipe`，没有任何栈指向真正原因。

**这就是为什么 `services/mcp_client.py` 每次调用都新建短连接**（代价约 1.15 秒/次）。
不要"优化"成长连接。

### 6. `mcp` 是 v2，网上教程都是 v1
```python
from mcp.server import MCPServer          # ✓ v2
from mcp.server.fastmcp import FastMCP    # ✗ ModuleNotFoundError，v1 的路径已【删除】
```
v2 的字段全是 snake_case（`tool.input_schema`、`result.is_error`），
`transport` 参数在 `run()` 上而不是构造函数上。
**只以官方 v2 文档为准，不要照抄博客。**

### 7. Redis 的两个连接坑（症状都是"特征层好像没接上"）
```python
Redis.from_url(settings.redis_url, decode_responses=True, protocol=2)   # ✓
Redis.from_url("redis://localhost:6379/0")                             # ✗ 两个坑都在里面
```

**坑 A —— 必须 `protocol=2`。** 本机 6379 上是原生 Redis **5.0**，它不支持 `HELLO`
命令，而 redis-py 5+ 默认走 RESP3、建连时先发 HELLO →
`ResponseError: unknown command 'HELLO'`。
RESP2 在 Redis 7.x 上同样合法，所以这不是"只在本机能跑的 hack"。

**坑 B —— URL 必须写 `127.0.0.1`，不能写 `localhost`。**
`localhost` 会先解析到 IPv6 的 `::1`，而本机 Redis 只监听 IPv4 ——
首次连接在 `::1` 上挂约 2 秒才回落。实测首次 PING：
`localhost` **2052.3 ms** / `127.0.0.1` **2.4 ms**。

这个 2 秒大于请求路径的超时（`feature_store_timeout_s` = 0.5），于是连接会在
建立中途被掐断 —— **每次请求都重新发起连接、每次都超时**，`source` 永远是
`fallback`，看起来就像"特征层根本没接上"。

所以 `main.py` 的 lifespan 里有一次 `warmup()`，把首次连接的代价在启动时付掉。

### 8. `Settings` 是 `extra="forbid"` —— 删配置项会让旧 `.env` 起不来

删掉 `milvus_host` / `database_url` 这类死配置项之后，如果 `.env` 里还留着
对应的 `ECOM_*` 键，服务**启动直接失败**：

```
ValidationError: ecom_milvus_host  Extra inputs are not permitted
```

这不是 bug，是**故意**保留的严格模式。不要为了"宽容"把它改成
`extra="ignore"`：静默忽略认不出的配置，意味着一个拼错的变量名
（`ECOM_FEATURE_STORE_ENABLE` 少个 D）不报错、直接走默认值 ——
那正是本文档反复强调的"静默读不到配置"那一类最难查的坑。
宁可启动失败并明确指出是哪一项。

**推论**：以后每次删配置项，都要同步检查 `.env` 与 `.env.example`。

### 9. compose 里 `environment` 的优先级【高于】`env_file`

写 `ECOM_LLM_API_KEY=${ECOM_LLM_API_KEY:-}` 这种"带默认值的兜底"时，
如果 shell 里没有该变量，它展开成**空串**，然后**覆盖掉 `.env` 里的真实值**。

表面上看 compose 文件"配得很全"，实际把 API Key 抹成了空 ——
而且不报任何错。这个坑是靠 `docker compose config` 看到
`ECOM_LLM_API_KEY: ""` 才发现的。

**规则**：`environment` 里只放"容器内必须与宿主机不同"的项
（目前只有 `ECOM_REDIS_URL`，要用服务名而不是 127.0.0.1）。
其余一律留给 `.env`，不要在 compose 里重复声明。

排查手法：改完 compose 先跑 `docker compose config`，
它会把**插值后的最终值**打出来 —— 这是唯一能看穿上面那个坑的地方。

### 10. 兜底的 500 处理器读不到 request_id（contextvar 已被复位）

顺序问题：Starlette 的 `ServerErrorMiddleware` 永远是**最外层**，
用户中间件包不住它。异常从内层向上穿过 `RequestIdMiddleware` 的
`with request_context(...)` 时，`contextvar` 就已经复位了 ——
等 `ServerErrorMiddleware` 调我们的兜底处理器时，
`current_request_id()` 返回 `None`，500 响应体里的 request_id 是空的。
**而那恰恰是最需要它的时候。**

**解法**：`RequestIdMiddleware` 除了设 contextvar，还往
`scope["state"]` 放一份；错误处理器的 `_request_id_of()` **先读 scope、
再回落 contextvar**。scope 是普通 dict，不随栈展开失效。

同理，兜底处理器还得**自己**加 `X-Request-ID` 响应头：
其它三个处理器（AppError / 校验 / HTTPException）由 `ExceptionMiddleware`
调用，在 `RequestIdMiddleware` 内层，响应会经过我们的 send 包装；
只有 500 这条绕过了包装。

回归测试：`tests/test_api.py::test_unhandled_exception_is_normalized`
（它就是这条修复留下的钉子）。

### 11. 在 import 期构造外部客户端 ⇒ 缺凭据时连测试都跑不起来

这是本项目**最隐蔽**的一个结构性问题，也是建 CI 时才被发现的：
全新 clone（没有 `.env`）跑测试，报的是

```
ERROR tests/test_api.py - openai.OpenAIError: Missing credentials
```

**不是断言失败，是测试根本收集不起来。** 根因链条：

```
import main
  → main.py 模块级 supervisor = get_supervisor()
    → get_agents() 构造 4 个 Agent
      → build_chat_model() → ChatOpenAI(...)
        → openai SDK 在【构造期】校验凭据，缺了直接抛
```

而 `harness/deps.py` 的 docstring 里写得很清楚，用 `@lru_cache` 而不是模块级全局
**正是为了**"MCP Server 进程、测试进程未必需要全部依赖" ——
`main.py` 那次模块级调用把惰性设计抵消掉了。

**两条规则**（都已落地，改动前先读一遍）：

1. **不要在模块级（import 期）构造任何需要凭据/网络的客户端。**
   需要共享单例就用 `@lru_cache` 的取值函数，并且**在函数体内调用**它
   （`main.py` 现在是在路由里调 `get_supervisor()`；那是字典查找，没有代价）。
   想知道有没有踩：`python -c "import main"` 在没有 `.env` 的目录里应当成功。
2. **缺凭据不该致命，但必须大声。**
   `harness/llm.py:_resolve_api_key()` 在 key 为空时返回占位符并报一次 error，
   行为等价于"key 过期/被吊销"→ 调用时 401 → Agent 走 fallback，
   也就是本来就设计好的降级路径（故障注入实测 4/4 返回 200）。
   `lifespan` 里还有一条 `app.llm_key_missing` 提示处置方法。

**CI 里有守卫**：`.github/workflows/ci.yml` 显式设 `ECOM_LLM_API_KEY: ""`。
谁再把凭据变成测试的前提，CI 会红。

---

## 四、项目结构

```
backend/
├── main.py                  FastAPI 入口 + 6 个路由 + lifespan
├── logging_setup.py         ★ structlog 配置（JSON 输出）。启动时调一次
├── pyproject.toml           工具配置：pytest（testpaths/addopts）+ 覆盖率门禁
├── config/settings.py       pydantic-settings，env_prefix="ECOM_"，@lru_cache，extra="forbid"
├── web/                     ★ 接入层：中间件装配 + 类型化错误 + 全局异常处理器
│   ├── __init__.py          install_http_layer()：顺序 路由→鉴权→限流→请求ID→CORS
│   ├── middleware.py        RequestIdMiddleware（X-Request-ID）/ ApiKeyMiddleware
│   ├── ratelimit.py         滑动窗口限流（默认关闭）
│   └── errors.py            AppError / 4 个 exception_handler / 统一错误信封
├── agents/                  4 个 Agent，都继承 base_agent.BaseAgent
│   ├── base_agent.py        ★ harness 的接线点：熔断门→超时→重试→降级
│   ├── user_profile_agent.py  (LLM) 抽用户画像
│   ├── product_rec_agent.py   (LLM) 召回 + 重排；MOCK_PRODUCTS 在这里
│   ├── marketing_copy_agent.py(LLM) 生成文案
│   └── inventory_agent.py      (无 LLM) 库存过滤；★ MCP 的唯一接线点
├── harness/                 ★ 运行时骨架，不含业务
│   ├── trace.py             request_id 贯穿（structlog contextvars）
│   ├── breaker.py           熔断器（纯逻辑，零依赖，最厚的单测在这）
│   ├── runtime.py           按 agent 名索引的共享熔断状态 + 调用顺序
│   ├── llm.py               build_chat_model()：provider 调优集中处
│   │                        ★ _resolve_api_key()：缺凭据不致命（见陷阱 11）
│   └── deps.py              组合根：全进程一份实例（**惰性**，见陷阱 11）
├── orchestrator/
│   ├── supervisor.py        三阶段并行编排（/api/v1/recommend 用这个）
│   ├── graph.py             LangGraph 版同一件事（/api/v1/recommend/graph）
│   │                        ★ build_response() 归一化键名，与主接口同契约
│   └── reporting.py         ★ 两条编排路径共用的 HarnessReport 组装
├── mcp_servers/
│   ├── wms_server.py        MCP Server：SQLite 库存，4 工具 + 1 resource
│   └── init_wms_db.py       建表 + 种子（★ 种子故意与 Product.stock 不一致）
├── services/
│   ├── metrics.py           内存指标
│   ├── mcp_client.py        MCP 短连接客户端，永不抛异常给调用方
│   └── feature_store.py     Redis 实时特征（可选依赖，默认关闭；同契约：永不抛）
├── scripts/
│   └── seed_behavior.py     灌行为种子数据（照 init_wms_db.py 的模式）
└── tests/                   232 个测试（231 passed + 1 skipped），全部离线
    ├── test_api.py          ★ 接口级测试：6 条路由 + 错误信封 + 中间件
    └── test_concurrency.py  ★ 并发正确性（守卫 breaker.py 的"不需要锁"声明）
```

根目录另有 `.github/workflows/ci.yml`（两个 job：测试+覆盖率门禁、
`docker compose config`）。

---

## 五、改代码时的约定

### 加一个新 Agent
1. 继承 `BaseAgent`，实现 `async def _execute(**kwargs) -> XxxResult`
2. **不要**在 `__init__` 里直接 `ChatOpenAI(...)` —— 用 `harness.build_chat_model(name, ...)`
   （有测试 `test_agents_do_not_construct_chatopenai_directly` 守着这条）
3. 注册进 `harness/deps.get_agents()`
4. 在 `models/schemas.py` 加 `XxxResult(AgentResult)`

### 改 `BaseAgent` 时注意
`run()` 里的**顺序不能动**：`熔断门 → wait_for → 重试 → 记账`。
- 熔断必须在重试**外层**（内层会让每个 attempt 重新求值，且放大失败信号）
- `self.timeout` 是**整个 run() 的总预算**，不是单次尝试预算

### 加配置项
写在 `config/settings.py`，同时更新 `backend/.env.example`。
**新能力默认值一律 `false`** —— 保证现有链路零破坏。
（对齐的先例：`mcp_enabled`、`mcp_wms_enabled`、`feature_store_enabled`、
`api_key_enabled`、`dev_reload`、`rate_limit_enabled` —— 全部默认 false。）

⚠️ `Settings` 是 `extra="forbid"` 的，见陷阱 8。

### 删配置项
**必须同步清理 `.env` 与 `.env.example`**，否则本机与容器的服务都会
**启动失败**（`ValidationError: Extra inputs are not permitted`）。

### 加依赖
`requirements.txt` 顶部有维护规则：每一项都必须有【代码里的真实引用】，
或有明确的传递性理由。写完先 grep 一遍确认引用存在。

背景：这个文件曾挂着 5 个零引用的包，`docker-compose.yml` 还为此起了
两个容器 —— 而代码里一行都没用。半个功能比没有功能更糟：
它把注意力引到最薄的地方，而这与本文档第六节的"诚实红线"直接冲突。

### 加路由
必须在 `tests/test_api.py` 里加一条用例 —— 至少覆盖：状态码、
响应形状、以及错误路径的错误信封。
（加这个文件之前，5 条路由的测试覆盖是 0。路由签名改了、响应模型漏了字段，
168 个单测全绿，而接口已经坏了。）

另外：**要么声明 `response_model`，要么明确说明为什么不需要**。
`/api/v1/recommend/graph` 曾经漏了它，后果是 OpenAPI 里没有 schema、
前端生成不了类型，而且返回结构与主接口不一致 —— 前端得为同一个
"推荐"概念写两套解析。

### 两条推荐路径要保持同一个契约
`supervisor.py` 与 `graph.py` 产出的是同一个 `RecommendationResponse`，
`agent_results` 的键名必须一致（`orchestrator/reporting.py` 里有规范键名）。
新增 Agent 时两条路径**都要**改，`tests/test_api.py` 里有条断言专门
比较两边的顶层键集合。

### 改完代码
CI 会跑 `compileall` + `pytest --cov`（门槛 83）与 `docker compose config`。
新增代码不补测试会让覆盖率掉下去并让 CI 红。

### 不要在主流程的 import 期构造外部客户端
见陷阱 11 —— 它会同时打掉测试、CI 和一个全新 clone 的人。

### 加中间件
改 `web/__init__.py` 的 `install_http_layer()`，不要在 `main.py` 里
散着 `add_middleware`。顺序有硬要求（后 add 的在外层，见该文件 docstring），
写反了不报错，只会让 401 丢掉 CORS 头、或者鉴权失败没有 request_id。

### 改响应模型时注意
`models/schemas.py` 的 `agent_results` 声明为 `dict[str, SerializeAsAny[AgentResult]]`。
**不要动它** —— 两个方向都会坏：

- 改回裸的 `dict[str, AgentResult]` → pydantic 按【声明类型】序列化，
  子类字段（`profile`/`products`/`copies`/…）会被**静默截断**
- 改成联合类型（`UserProfileResult | ProductRecResult | …`）→
  `BaseAgent._fallback()` 在超时/熔断时返回的是**基类** `AgentResult`，
  联合类型会让这条降级路径校验失败（故障注入的 4/4 HTTP 200 会变成 500）

回归测试：`tests/test_response_schema.py`。新增 Agent 时这里**不用改**。

### 注释风格
中文，写**为什么**而不是**是什么**。
如果某个决策是踩坑得出的，把**坑写进注释**（本项目大量采用这种风格）。

---

## 六、这个项目的"诚实红线"

`README.md` 里有一批**未经实测的数字**（CTR +15%、文案点击率 +23%、P99<2s、三语言实现）。
它们是早期写的，**与代码实际不符**。清理它们是待办事项（见 progress.md 的 M8）。

**在那之前**：
- 不要基于 README 的数字做技术判断
- 不要把这些数字写进新的文档或简历
- 本项目**唯一可信的数字来源**是 [docs/progress.md](docs/progress.md) 里标注了"实测"的那些

具体哪些是假的，[docs/progress.md](docs/progress.md) 的「已知问题」一节列了清单。
