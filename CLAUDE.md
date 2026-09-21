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

### 解释器：只能用绝对路径

```bash
# ✓ 正确
python/.venv/Scripts/python.exe -m pytest tests/ -q

# ✗ 错误 —— 会命中根目录那个【空的假 venv】
.venv/Scripts/python.exe -m pytest
```

**这台机器上有 11 个 Python 解释器，其中 6 个目录名叫 `.venv`。**
根目录的 `.venv` 是废的（site-packages 是空的），真正的在 `python/.venv`。
用错的表现是 `ModuleNotFoundError: No module named 'xxx'`，而你以为装了。

### 工作目录：必须在 `python/`

```bash
cd python && ./.venv/Scripts/python.exe -m pytest tests/ -q
```

原因：`config/settings.py:38` 用的是**相对路径** `env_file=".env"`，
`main.py:17` 也有 `sys.path.insert(...)`。cwd 不对 ⇒ **静默读不到配置**（不报错，直接用默认值）。

### 在别处跑脚本：要设 PYTHONPATH

```bash
cd python && PYTHONPATH="D:/devlop/multi-agent-ecommerce-system/python" \
  ./.venv/Scripts/python.exe /d/tmp/some_script.py
```

Python 把**脚本所在目录**加进 `sys.path`，不是 cwd。

### 起服务

```bash
cd python && ./.venv/Scripts/python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000 --no-access-log
```

**调试和演示时加 `--no-access-log`，并且【不要】加 `--reload`**（原因见陷阱 5）。

### 初始化 MCP 的库存数据库

```bash
cd python && ./.venv/Scripts/python.exe mcp_servers/init_wms_db.py --reset
```

---

## 三、六个陷阱（都是实际踩过的）

### 1. 假的根目录 `.venv`
见上文。**永远用 `python/.venv/Scripts/python.exe` 的绝对/相对完整路径。**

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

---

## 四、项目结构

```
python/
├── main.py                  FastAPI 入口 + 4 个路由
├── config/settings.py       pydantic-settings，env_prefix="ECOM_"，@lru_cache
├── agents/                  4 个 Agent，都继承 base_agent.BaseAgent
│   ├── base_agent.py        ★ harness 的接线点：熔断门→超时→重试→降级
│   ├── user_profile_agent.py  (LLM) 抽用户画像
│   ├── product_rec_agent.py   (LLM) 召回 + 重排；MOCK_PRODUCTS 在这里
│   ├── marketing_copy_agent.py(LLM) 生成文案；唯一【保留推理】的 Agent
│   └── inventory_agent.py      (无 LLM) 库存过滤；★ MCP 的唯一接线点
├── harness/                 ★ 运行时骨架，不含业务
│   ├── trace.py             request_id 贯穿（structlog contextvars）
│   ├── breaker.py           熔断器（纯逻辑，零依赖，最厚的单测在这）
│   ├── runtime.py           按 agent 名索引的共享熔断状态 + 调用顺序
│   ├── llm.py               build_chat_model()：provider 调优集中处
│   └── deps.py              组合根：全进程一份实例
├── orchestrator/
│   ├── supervisor.py        三阶段并行编排（/api/v1/recommend 用这个）
│   └── graph.py             LangGraph 版同一件事（/recommend/graph 用）
├── mcp_servers/
│   ├── wms_server.py        MCP Server：SQLite 库存，4 工具 + 1 resource
│   └── init_wms_db.py       建表 + 种子（★ 种子故意与 Product.stock 不一致）
├── services/
│   ├── ab_test.py           分桶 + Thompson 采样
│   ├── metrics.py           内存指标
│   └── mcp_client.py        MCP 短连接客户端，永不抛异常给调用方
└── tests/                   144 个测试，全部离线
```

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
写在 `config/settings.py`，同时更新 `python/.env.example`。
**新能力默认值一律 `false`** —— 保证现有链路零破坏。

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
