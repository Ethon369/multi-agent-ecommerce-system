# 阶段 D：MCP Server 侧 —— 把项目开放出去

> 上一阶段：[03-tool-registry.md](03-tool-registry.md) · 下一阶段：05-ops-copilot.md
>
> 使用文档（怎么配、怎么验）见 [../mcp-integration.md](../mcp-integration.md)。
> 这篇讲**为什么这么设计**。

---

## 一、这一步补上的是什么

前面几个阶段做的都是「项目**消费**外部能力」：

```
本项目 ──(MCP Client)──> wms_server
```

现在要反过来 —— **把项目能力暴露出去**：

```
Claude Desktop / Cursor ──(MCP Client)──> recommend_server ──> 本项目的推荐能力
```

**两个方向合起来才叫「MCP 双侧」。** 只有 Client 侧的话，
你证明的是"我会用别人的服务"；加上 Server 侧，才证明"我能把我的系统
改造成别人能用的形态"。

## 二、小白版原理：Client 和 Server 到底差在哪

| | Client 侧（阶段 B 之前就做了） | Server 侧（本阶段） |
|---|---|---|
| 谁发起 | **我**主动去问别人 | **别人**来问我 |
| 类比 | 你打电话给餐厅订位 | 餐厅挂出菜单，等别人打来 |
| 难点 | 别人挂了怎么办（降级） | 别人问的格式要标准（契约） |
| 本项目 | 库存 Agent 问 WMS | Claude Desktop 问推荐系统 |

**关键点：Server 侧不是"新写一份推荐逻辑"，只是"换个协议壳"。**

这在本项目里体现得很明显 —— `recommend_server.py` 里**没有一行业务逻辑**，
全部是 `from harness.deps import get_supervisor` 然后调用。
所以 REST 接口和 MCP 接口看到的是**同一份数据**，不会出现"两套真相"。

## 三、代码怎么实现的

### `python/mcp_servers/recommend_server.py` — MCP Server

**面试考点**: 协议壳与业务分离、pydantic 序列化陷阱、工具描述的作用

```python
server = MCPServer(
    name="ecommerce-recommend",
    version="1.0.0",
    instructions=(
        "……\n"
        "重要：recommend_products 会真实调用大模型，**单次约 5-15 秒**，"
        "请预留足够超时时间，不要当成毫秒级的本地查询。\n"
        "除 record_experiment_outcome 外，全部工具都是只读的。"
    ),
)
```

**为什么 `instructions` 里要强调"5-15 秒"**：
Host 侧的模型看到这个工具，默认会按"本地函数"的预期给它一个短超时。
不写明的话，它会超时、判定失败、然后可能重试 —— 而每次重试都要真金白银再跑一遍。

同样的理由，工具的 `description` 里也写了一遍。

### 唯一一个写操作，以及为什么选它

```python
@server.tool(description="记录一次 A/B 实验的结果……本服务【唯一】的写操作")
async def record_experiment_outcome(experiment_id: str, group: str, success: bool):
    engine = get_ab_engine()
    if experiment_id not in engine.experiments:
        # 返回可判定的结果而不是抛异常 —— 让调用方（包括模型）能自行处置
        return {"ok": False, "error": "experiment_not_found",
                "available": sorted(engine.experiments)}
    engine.record_outcome(experiment_id, group, success)
    return {"ok": True, ...}
```

选它当唯一写操作的理由：**它只累加计数，是幂等安全的**。
Host 侧的模型误触发一次，后果只是多一个样本，不会破坏数据。

### 扁平返回结构：绕开一个 pydantic 陷阱

**面试考点**: 声明类型序列化、为什么不能直接 `model_dump()`

```python
def _flat_response(payload: dict) -> dict:
    """
    ⚠️ 为什么不能直接返回 RecommendationResponse.model_dump()
    ─────────────────────────────────────────────────────
    models/schemas.py 里 agent_results 声明的是 dict[str, AgentResult]，
    pydantic 会按【声明类型】序列化，于是子类独有字段
    （profile / products / copies / available_products / low_stock_alerts）
    会被静默截断 —— 实测响应里完全看不到。
    """
    return {
        "request_id": ...,
        "products": [...],          # ← 扁平的，模型不用猜嵌套
        "marketing_copies": ...,
        "usage": ..., "agents": ...,
    }
```

**这不是理论问题**：本项目的 HTTP 接口就有这个 bug（`/api/v1/recommend`
的响应里看不到 `agent_results` 内部的子类字段），所以 `/recommend/graph`
端点早就用了同样的"手工扁平化"绕法。这里沿用同一思路。

**顺带的好处**：扁平结构对模型更友好 —— 信息都在顶层，不用猜嵌套路径。

### 边界校验放在调 LLM 之前

```python
if num_items < 1:
    return {"ok": False, "error": "num_items_must_be_positive"}
if num_items > 20:
    return {"ok": False, "error": "num_items_too_large", "max": 20}
```

**校验必须在【调用之前】** —— 否则一个离谱的参数会先把整条流水线跑完
（约 10 秒 + 产生真实费用），才在最后被拒绝。

## 四、一个实测出来的重大取舍：文案要不要保留推理

这个发现来自阶段 B 的账本 + 一次 A/B 实测，值得单独讲。

### 账本先指了方向

阶段 B 的账本显示：

```
user_profile     out=  101  reasoning=   0
product_rec      out=   20  reasoning=   0
marketing_copy   out= 2664  reasoning=2436    ← 占全部输出 token 的 96%
```

**`marketing_copy` 一个 Agent 占了 96% 的输出 token、89% 的成本。**
而它正是我此前**故意保留推理**的那一个（理由是"创作型任务，思考可能换来更好的文案"）。

### 于是做了 A/B

同一份代码，只改一个环境变量（`ECOM_LLM_THINKING_EXEMPT_AGENTS=""` = 谁都不豁免），
各跑 3 次：

| | A 保留推理 | B 全部关推理 | 改善 |
|---|---|---|---|
| 全链路 p50 | 6,436 ms | **2,596 ms** | **2.5×** |
| `marketing_copy` | 4,890 ms | **1,229 ms** | **4.0×** |
| 成本 p50 | $0.001604 | **$0.000472** | **3.4×** |
| 输出 token | 1,118 | 243 | 4.6× |
| 推理 token | 878 | 0 | — |
| 文案条数 | 3/3/3 | 3/3/3 | 相同 |

### 但质量必须人来看

| | A 保留推理 | B 关推理 |
|---|---|---|
| P003 | 「主动降噪**隔绝喧嚣**，无线聆听**纯净天籁**」 | 「主动降噪，无线自由聆听」 |
| P004 | 「头戴降噪旗舰，**静享殿堂级音质**」 | 「头戴降噪旗舰。**静界由您掌控**」 |
| P006 | 「**旗舰大屏与沉浸影音**，娱乐办公皆从容」 | 「**轻奢娱乐伴侣**。高性价比之选」 |

**诚实评价**：A 组明显更贴合具体商品（"殿堂级音质"、"旗舰大屏"是产品特征），
B 组更泛化（"卓越音质"换个耳机也说得通）。但两组都没有语法错误、没有违禁词、
都是合格文案 —— **差距是"好 vs 更好"，不是"能用 vs 不能用"**。

### 结论与做法

**保持推理开启**（营销文案的产出质量就是它这份工作的全部价值），
但**把这个取舍连同实测数字一起写下来**，并保持一行配置就能翻转：

```bash
# 想要 2.5 倍速度与 3.4 倍成本优势，代价是文案更泛化：
ECOM_LLM_THINKING_EXEMPT_AGENTS=
```

> **这就是记账的价值**：它把一个主观问题（"文案需不需要推理"）
> 变成了一个**有数字、可复现、可回退**的工程取舍。

## 五、怎么验证的

### 1. 进程内测试（`python/tests/test_recommend_server.py`，10 条）

用 `Client(server)` 直连 —— **不起子进程、不占端口**（mcp v2 的新能力，
v1 需要 `create_connected_server_and_client_session()`，v2 已移除）。

| 测试 | 守住什么 |
|---|---|
| `test_all_tools_discoverable` | A1：4 个工具都能被发现，且都有 description |
| `test_recommend_products_documents_its_slowness` | 描述里必须写明"秒"，否则 Host 会给短超时 |
| `test_record_outcome_is_idempotent_safe` | 写操作的幂等安全性 |
| `test_record_outcome_unknown_experiment_is_a_value` | 未知实验返回【值】而非异常，且告诉模型有哪些可选 |
| `test_num_items_bounds`（3 个参数化用例） | 边界校验在调 LLM **之前** |
| `test_recommend_products_full_path` | 完整链路（真调 LLM，默认跳过，设 `RUN_SLOW_MCP_TESTS=1` 才跑） |

最后一条重点验证**扁平结构绕开了截断** —— 顶层能直接看到商品和文案。

### 2. 实测输出

```
is_error=False  耗时=13386ms
顶层字段: ['agents', 'experiment_group', 'marketing_copies',
          'products', 'request_id', 'total_latency_ms', 'usage', 'user_id']
products: ['P006', 'P003', 'P005']
copies: 3
usage: calls=3 in=871 out=2327 cost=$0.003054

参数校验:
  num_items=0  -> {'ok': False, 'error': 'num_items_must_be_positive'}
  num_items=99 -> {'ok': False, 'error': 'num_items_too_large', 'max': 20}
```

`pytest tests/ -q` → **127 passed**（阶段 C 之后 117，本阶段新增 10）

## 六、这一步在简历上怎么写

> 基于 MCP Python SDK v2 实现 Server 侧：将推荐、实验、指标能力封装为
> 4 个 MCP 工具（含 1 个幂等安全的写操作）暴露给外部 AI 工具调用，
> 与既有的 Client 侧构成 MCP 双侧；协议层与业务层完全分离（Server 内零业务逻辑，
> 复用同一组单例），并针对 pydantic 声明类型序列化的截断问题显式构造扁平返回。

**为什么这条值得写**：
① 它证明的不是"会用 MCP"，而是**能把一个已有系统改造成可被外部消费的形态**；
② "协议壳与业务分离"是一个**架构判断**，且能拿出证据（Server 里没有业务逻辑）；
③ 连同阶段 B 的账本、阶段 C 的工具层，串成了一条完整的设计链 ——
   **面试时可以顺着讲 20 分钟。**
