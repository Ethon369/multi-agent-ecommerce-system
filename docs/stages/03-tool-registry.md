# 阶段 C：工具层（ToolSpec / ToolRegistry）

> 上一阶段：[02-token-accounting.md](02-token-accounting.md) · 下一阶段：04-mcp-server-side.md
>
> 一句话：**让"内置函数"和"MCP 工具"对调用方长得一样，把超时/重试/降级/记录只写一遍。**

---

## 一、为什么需要这一层

项目里现在有两类"能力"：

| 类型 | 例子 | 从哪来 |
|---|---|---|
| 内置 Python 函数 | 查指标、查实验、列商品目录 | 本项目代码 |
| MCP 工具 | 查库存、批量查库存、列低库存 | 一个会起子进程的外部 Server |

如果各写一遍，就会有**四份**重复的"超时 / 重试 / 降级 / 记账 / 记日志"代码。
更糟的是：将来接第 3 类能力时，**一定会漏掉其中某一项，而且漏了不会有任何报错**。

所以要有一个统一的契约。

## 二、小白版原理：什么是"注册表"

打比方：**餐厅的点菜单**。

- 每道菜（工具）有：名字、介绍（描述）、需要什么配料（参数）
- 服务台（注册表）拿着这本菜单
- 谁来点菜（模型 / 代码），都按同一套流程走：下单 → 做菜 → 超时了怎么办 → 做不出来怎么替代

**关键点是"流程统一"**：不管这道菜是后厨现做的（内置函数），
还是从外面叫的外卖（MCP 工具），**超时了怎么办、做砸了怎么办，规则是一样的**。

## 三、一条不能越过的线

先说清楚这一层**不做什么**，否则很容易被理解成 ReAct 框架：

> **注册表是「带韧性保障的分发表」，不是 agent 框架。**
>
> 主推荐链路里的 4 个 Agent 调用工具是**按名字写死**的：
> `await self.db.batch_query_stock(...)` —— **模型不参与"调哪个工具"的决策**。
>
> 唯一由模型自主选工具的地方是运营 Copilot（阶段 E）。
> 这条界线是刻意的：**确定性场景不该引入 ReAct 的不确定性。**

面试时大概率会被问"这不就是 ReAct 吗" —— 上面这段就是答案。

## 四、代码怎么实现的

### `python/harness/tools/spec.py` — 工具契约

**面试考点**: 契约设计、ok 与 degraded 的分离、重试范围的取舍

```python
@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str          # 会原样送进模型的工具列表 —— 模型靠它决定调不调
    input_schema: dict        # JSON Schema，刻意用 MCP 的【原生形状】
    handler: Callable[..., Awaitable[Any]]

    timeout_s: float = 5.0    # 【单次尝试】的超时，不是总预算
    max_attempts: int = 1     # 默认 1 = 不重试

    retry_on: tuple[type[BaseException], ...] = (TimeoutError, ConnectionError)
    fallback: Callable[..., Any] | None = None
    tags: frozenset[str] = frozenset({"read_only"})
    source: str = "builtin"   # 仅用于观测：builtin / mcp:wms
```

**三个设计取舍**：

| 取舍 | 理由 |
|---|---|
| 默认**不重试**（`max_attempts=1`） | 一个超时 3 秒的只读工具重试两次，会在一条本来就 16 秒的链路上再烧 6 秒，换来的还是同一个答案。重试要**按工具逐个决定** |
| 只重试 `TimeoutError` / `ConnectionError` | 业务异常（比如商品 ID 不存在）重试多少次都是同样结果，白花时间 |
| handler **统一要求 async** | 同步函数在注册时包一层即可（`fn_to_async`），这样注册表内部不用做同步/异步分支 |

结果对象：

```python
@dataclass
class ToolResult:
    ok: bool
    value: Any = None
    error: str | None = None
    source: str = "builtin"
    latency_ms: float = 0.0
    attempts: int = 1
    degraded: bool = False     # ← 关键字段
```

> **`ok` 与 `degraded` 为什么要分开**
>
> 降级成功时：`ok=True`（调用方可以继续），`degraded=True`（但要知道这是次优结果）。
>
> 例：WMS 超时了，回落到本地快照 —— 这是 `ok=True` + `degraded=True`。
> 如果只有 `ok` 一个字段，这种"用旧数据顶上了"的情况就没法表达，
> **要么谎报成功、要么谎报失败，两个都不对**。

### `python/harness/tools/registry.py` — 注册表

**面试考点**: 失败即值、未知工具的容错、不抛异常的一致性

```python
async def call(self, name: str, **kwargs: Any) -> ToolResult:
    """调用一个工具。**永不抛异常。**"""
    spec = self._tools.get(name)
    if spec is None:
        # 模型会幻觉出不存在的工具名。这必须是【值】而不是 KeyError ——
        # 否则一次幻觉就能把整条请求打挂。
        return ToolResult(ok=False, error=f"unknown_tool:{name}", attempts=0)

    for attempt in range(1, spec.max_attempts + 1):
        try:
            async with asyncio.timeout(spec.timeout_s):
                value = await spec.handler(**kwargs)
            return ToolResult(ok=True, value=value, ...)
        except Exception as exc:
            if attempt < spec.max_attempts and isinstance(exc, spec.retry_on):
                await asyncio.sleep(0.2 * attempt)
                continue
            break

    # 尝试都失败了 -> 试降级
    if spec.fallback is not None:
        value = spec.fallback(**kwargs)
        return ToolResult(ok=True, value=value, error=last_error, degraded=True)

    return ToolResult(ok=False, error=last_error, ...)
```

**为什么 `call()` 永不抛异常**：这与 `BaseAgent.run()` 的既有约定一致，
让每个调用点从 `try/except` 变成一句 `if not res.ok`。

**一个细节**：`asyncio.CancelledError` 是 `BaseException`，**不会**被
`except Exception` 抓到 —— 这是**对的**，客户端断连应该向上传播，
而不是被降级掩盖掉。

### `python/harness/tools/mcp_source.py` — MCP 工具接入

**面试考点**: 协议形状的巧合、写操作的保守识别、子进程成本

```python
async def build_mcp_tools(server_key: str, *, timeout_s=6.0, expose_writes=False):
    raw_tools = await list_server_tools(server_key)

    for t in raw_tools:
        if not _is_read_only(t.name) and not expose_writes:
            continue      # 默认【不】把写操作暴露给模型

        specs.append(ToolSpec(
            name=f"{server_key}__{t.name}",     # 加前缀避免与内置工具重名
            description=f"[{server_key} 库存系统] {t.description}",
            input_schema=t.input_schema,        # ← 直接搬，零转换
            timeout_s=timeout_s,                # 比内置工具宽，因为要起子进程
            max_attempts=1,                     # 每次尝试起一个子进程，重试代价高
            source=f"mcp:{server_key}",
        ))
```

**这里最值得讲的一点：MCP 工具的 schema 不需要任何转换。**

MCP v2 的 `Tool` 对象字段是 `name` / `description` / `input_schema`，
而 `input_schema` 本身就是标准 JSON Schema —— **和我们要的形状是同一个东西**。

> 这也正是**「不需要 `langchain-mcp-adapters`」的技术依据**：
> 那个库的功能是把 MCP 工具转成 LangChain Tool 对象，
> 而 `langchain-core` 的 `convert_to_openai_function` 原生就认 MCP 这个形状
> （本阶段开始前已实测：`bind_tools` 直接接受，不用写一行转换代码）。

**写操作的识别是保守的**：

```python
def _is_read_only(tool_name: str) -> bool:
    # 拿不准的一律当写操作 —— 漏判的后果是"写操作被暴露给了模型"
    return tool_name.startswith(("query_", "list_", "get_", "search_", "batch_query_"))
```

为什么不问 Server？MCP 的 `Tool` 有 `annotations` 字段可以声明，
但我们自己的 `wms_server` 没填。**与其依赖一个可选字段，不如用一个显式的白名单。**

## 五、实测结果

```
=== 注册的工具（MCP 开启）===
  get_experiments                  builtin      timeout=3.0s
  get_metrics                      builtin      timeout=3.0s
  list_products                    builtin      timeout=3.0s
  wms__batch_query_stock           mcp:wms      timeout=6.0s
  wms__list_low_stock              mcp:wms      timeout=6.0s
  wms__query_stock                 mcp:wms      timeout=6.0s

（upsert_stock 被正确拦下 —— 它是写操作）

=== 调用 ===
  list_products (内置)         ok=True   23.9 ms
  wms__batch_query_stock (MCP) ok=True   1161.3 ms   ← 与实测的子进程成本吻合
  no_such_tool (幻觉)          ok=False  error=unknown_tool:no_such_tool  ← 没有抛异常
```

`/api/v1/metrics` 新增 `tools` 段 —— **这是判断"MCP 到底接上没有"最快的办法**
（开着比关着多 3 个工具）。

## 六、怎么验证的

`python/tests/test_tools.py`，20 条，全部离线（用假工具，不起子进程）：

| 类别 | 守住的 | 代表测试 |
|---|---|---|
| 分发 | 返回值、重名拒绝 | `test_duplicate_name_rejected` |
| **容错** | 未知工具返回【值】而非异常 | `test_unknown_tool_is_a_value_not_an_exception` |
| 超时 | 真超时（0.05s 的工具不会跑 5s） | `test_timeout_is_enforced` |
| 重试 | 默认不重试、瞬时故障可重试、**业务异常不重试** | `test_business_errors_are_not_retried` |
| 降级 | `ok=True` + `degraded=True` 同时成立、降级失败时报原始错误 | `test_fallback_produces_degraded_success` |
| Schema | 直接吐 MCP 原生形状、不做转换 | `test_openai_schemas_uses_mcp_native_shape` |
| MCP | 写操作识别（7 个用例的参数化测试） | `test_write_tool_detection` |

`pytest tests/ -q` → **117 passed**（阶段 B 之后 97，本阶段新增 20）

## 七、这一步在简历上怎么写

> 设计并实现统一工具层：以 ToolSpec 契约统一内置函数与 MCP 工具，
> 将超时 / 重试 / 降级 / 观测收敛到单一分发点，避免每接一类新能力就重复一遍。
> 采用「失败即值」而非抛异常的分发约定，使模型幻觉出的工具名不会打挂请求；
> 并将 ok 与 degraded 分离，让"降级顶上"这种中间状态可表达、可观测。

**为什么这条值得写**：
① 它体现的是**抽象能力**（把重复的东西收敛到一处），不是写业务代码；
② "失败即值 vs 抛异常"是一个**有取舍的技术判断**，能展开讲；
③ 它是 Copilot（阶段 E）的前置，**串起了一条完整的设计链**。

---

## 附：这一层的文件

| 文件 | 职责 | 行数 |
|---|---|---|
| `harness/tools/spec.py` | ToolSpec / ToolResult / 同步转异步 | ~150 |
| `harness/tools/registry.py` | 注册、按标签筛选、分发、韧性保障 | ~180 |
| `harness/tools/builtin.py` | 内置工具（复用已有能力，不新增业务逻辑） | ~140 |
| `harness/tools/mcp_source.py` | MCP Server 工具 → ToolSpec | ~170 |
| `harness/tools/__init__.py` | `build_registry()` 组装 + 降级（没有 MCP 也能用） | ~35 |
