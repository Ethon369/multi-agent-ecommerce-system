# harness 和 MCP 到底体现在哪 —— 从零讲明白

> 这份文档是给**没读过那些代码的人**写的（首先是我自己）。
> 目标：读完之后，能在面试里用自己的话讲清楚这两件事，而不是背定义。
>
> 配套：[progress.md](progress.md)（进度与数字）· [code-walkthrough.md](code-walkthrough.md)（原有代码讲解）

---

# 第一部分：harness 到底长什么样

## 1.1 先说你已经熟悉的东西

你现在正在跟 AI 对话。你发一句话，它回你一段话。

但**模型本身只会做一件事**：根据你给的文字，猜下一段文字应该是什么。就这一件。

它**不会**：

| 你习以为常的事 | 模型自己会不会 |
|---|---|
| 读你磁盘上的文件 | ❌ 不会 |
| 执行一条命令 | ❌ 不会 |
| 记住你 10 分钟前说过什么 | ❌ 不会 |
| 工具调用失败了重试一次 | ❌ 不会 |
| 对话太长时自动压缩 | ❌ 不会（不压就爆了） |
| 一次改 10 个文件还不出错 | ❌ 不会 |

**让它能干这些事的那套代码，就叫 harness。**

> 所以 harness **不是**一个库、不是框架、不是某个特定文件。
> 它是**一类代码的统称** —— 就像"后厨"不是一道菜，是"做菜所必需的一整套东西"。
>
> 每个 LLM 应用都有 harness。区别只是：**你写了多少、写得好不好。**

## 1.2 你项目原来只有"半个" harness

你项目里有 4 个 Agent，其中 3 个会调 LLM。调用模型的代码，本质上是这样两行：

```python
response = await self.llm.ainvoke(messages)     # 调用模型
return json.loads(response.content)             # 解析结果
```

**这两行能跑，能出结果，演示完全没问题。**

但它**没有回答任何"如果……怎么办"**：

| 如果…… | 原来有答案吗 |
|---|---|
| 模型 60 秒不回怎么办？ | ❌ 没有。代码里写了 `self.timeout = 5.0`，但**整个项目没有一行读过它** |
| 调用失败要不要重试？ | ⚠️ 有重试，但**重试了你也不知道**（日志里什么都看不到） |
| 同一个 Agent 连续挂 10 次呢？ | ❌ 没有。`error_rate` 属性定义了，**从没被调用过** |
| 这次请求到底哪一步慢？ | ❌ 不知道。`request_id` 生成了，**然后就扔了** |
| 这一轮花了多少 token、多少钱？ | ❌ 完全没统计 |
| 改了 prompt，是变好了还是变差了？ | ❌ 没有评测，只能靠感觉 |

**"回答这些『如果……怎么办』的代码"，就是 harness。**

## 1.3 最有说服力的一击：凌晨 3 点排障

假设线上报警：**推荐接口大量超时**。你打开日志。

### 原来的日志

```
supervisor.start
（然后一片空白…… 23 秒后）
supervisor.complete   total_latency_ms=23000
```

**你能看出什么？什么都看不出来。**

这 23 秒里到底发生了什么？是哪个 Agent 慢？是在重试？是网络抖了？是模型抽风？**你只能猜**，或者去改代码加日志，然后等下次复现。

### 现在的日志

```
supervisor.start      request_id=abc
agent.success         agent=product_rec    request_id=abc  latency_ms=728
agent.retry           agent=user_profile   request_id=abc  attempt=1  error=...
agent.timeout         agent=user_profile   request_id=abc  timeout_s=5.0
agent.circuit_open    agent=user_profile   request_id=abc  state=open
supervisor.complete   request_id=abc  total_latency_ms=23000
```

**一眼就知道**：用户画像超时了 → 重试过一次 → 然后熔断了。

> 这不是"日志更好看"。**这是"你能不能干活"的区别。**

## 1.4 所以具体是哪几行代码

打开 [agents/base_agent.py](../python/agents/base_agent.py)，从第 48 行开始：

```python
async def _run_once(self, **kwargs: Any) -> AgentResult:
    runtime = get_runtime()

    # ① 熔断门：先问"这个 Agent 现在还能打吗"
    if not runtime.allow(self.name):                                   # :63
        return self._fallback(latency_ms, CircuitOpenError(...))       #     不能打就 0ms 返回

    try:
        # ② 超时：5 秒不回来就切断
        result = await asyncio.wait_for(                               # :71
            self._retry_execute(**kwargs),                             # ③ 里面是重试
            timeout=self.timeout,
        )
        runtime.record(self.name, True)                                # ④ 成功记一笔
        return result

    except TimeoutError:
        logger.error("agent.timeout", ...)                             # ⑤ 单独记"超时"
        runtime.record(self.name, False)                               #    失败也记一笔
        return self._fallback(latency_ms, TimeoutError(...))           # ⑥ 降级，不让异常往外抛

    except Exception as exc:
        logger.error("agent.failed", ...)
        runtime.record(self.name, False)
        return self._fallback(latency_ms, exc)
```

**关键在这里**：调用 LLM 本身，还是原来那两行，**一行都没变**。

变的只是**外面包了一层**。

```
        ┌─────────── 这层是 harness ───────────┐
        │  熔断门 → 超时 → 重试 → 记账 → 降级  │
        │   ┌─────── 这里是你原来的业务 ──────┐ │
        │   │  ainvoke(messages)              │ │
        │   │  json.loads(response.content)   │ │
        │   └─────────────────────────────────┘ │
        └───────────────────────────────────────┘
```

> **harness 不改变你的业务逻辑。它包在业务逻辑外面。**

## 1.5 一次请求从进来到返回（索引）

把上面的东西串起来，`POST /api/v1/recommend` 会走这些地方：

```
HTTP 请求进来
    │
    ▼
main.py 路由 (:73)
    │
    ▼
supervisor.recommend()                        ← 进入 request_context
    │                                            supervisor.py:70
    ├── 生成 request_id                          supervisor.py:56
    │
    ▼
Phase 1（两个 Agent 同时跑）                   asyncio.gather  supervisor.py:74
    ├── UserProfileAgent.run()
    │      └── _run_once()  ← 上面那六步        base_agent.py:48
    └── ProductRecAgent.run()
           └── _run_once()  ← 同样六步
    │
    ▼
Phase 2（两个 Agent 同时跑）                   supervisor.py:95
    ├── ProductRecAgent.run()  （重排）
    └── InventoryAgent.run()   ← 这里可能走 MCP（见第二部分）
    │
    ▼
Phase 3（必须等前面完成）
    └── MarketingCopyAgent.run()
    │
    ▼
拼装响应返回
```

**每个箭头都能指到具体文件的具体行** —— 这就是"harness 长什么样"。

---

# 第二部分：MCP 三个角色，用 P007 走一遍

## 2.1 先把三人组说清楚

| 角色 | 一句大白话 | 生活类比 |
|---|---|---|
| **Server** | 我**能提供**什么，挂出来给别人用 | 一家餐厅（菜单挂在门口） |
| **Client** | 我**去用**别人的服务 | 一个顾客（看菜单点菜） |
| **Host** | 我拿着模型，让它**自己决定**点哪些菜 | 一个帮你点菜的服务员（他自己看菜单决定） |

最容易混的是 **Client 和 Host**：

- **Client** 是代码写死的调用 —— "我就是要查库存，我去调库存服务"
- **Host** 是**让模型自己决定** —— "这是菜单，你自己看要点什么"

## 2.2 用 P007 这个商品走三个场景

先记住背景。**P007 是"Anker 140W充电器"**，它有两份互相矛盾的库存数据：

| 数据来源 | P007 的库存 |
|---|---|
| 代码里写死的假数据 `MOCK_PRODUCTS` | **2000**（有货） |
| WMS 真数据（SQLite 里的） | **0**（没货） |

这个矛盾是**故意造的**。原因见 2.5。

### 场景 A：MCP 关着（`ECOM_MCP_WMS_ENABLED=false`）

```
ProductRecAgent 召回了 P007
    │
    ▼
InventoryAgent._fetch_stocks()
    └── if not self.mcp_enabled:   ← inventory_agent.py:128
            return {}, "fallback"   ← 直接返回，根本没去问任何人
    │
    ▼
_resolve_stock(P007, 空表)
    └── 表里没有 → 用 Product.stock = 2000    ← inventory_agent.py:147
    │
    ▼
P007 有货 → 出现在推荐结果里
```

**此时谁是 Client？没有人。** 代码走的是"假数据"这条路。

响应里会看到：`"source": "fallback"`

### 场景 B：MCP 开着（`ECOM_MCP_WMS_ENABLED=true`）

```
InventoryAgent._fetch_stocks()
    └── if self.mcp_enabled:  ✓ 走这条路
            │
            ▼
        self.db.batch_query_stock([P001...P015])
            │
            │  ← 这里是 MCP Client（services/mcp_client.py）
            │     它启动一个子进程，用 stdio 说 MCP 协议
            ▼
        ┌─────────────────────────────────┐
        │  wms_server.py  ← 这里是 Server  │
        │  去查 SQLite：                  │
        │    SELECT stock FROM wms_stock  │
        │    WHERE product_id='P007'      │
        │  → 0                            │
        └─────────────────────────────────┘
            │
            ▼
        返回 {"P001":500, ..., "P007":0, ...}
    │
    ▼
_resolve_stock(P007, 表)
    └── 表里有值 0 → 用 0
    │
    ▼
P007 库存 0 → 被过滤掉，从推荐结果里消失
```

**此时**：`inventory_agent` 是 **Client**，`wms_server` 是 **Server**。

响应里会看到：`"source": "mcp"`

### 场景 C：MCP 开着但坏掉（比如数据库文件删了）

```
batch_query_stock(...)
    │
    ▼
    子进程起来了，但查库时抛异常
    │
    ▼
mcp_client._call() 捕获到 → 记一条 mcp.tool_error 日志 → 返回 None
    │                                （客户端承诺永不抛异常给调用方）
    ▼
_fetch_stocks() 收到 None → 记 inventory.mcp_degraded → 返回 {}, "fallback"
    │
    ▼
_resolve_stock(P007, 空表) → 回落 Product.stock = 2000
    │
    ▼
P007 又出现了！
```

**此时**：Client 还是 Client，Server 挂了，但**主接口仍然返回 HTTP 200**。

响应里会看到：`"source": "fallback"`

## 2.3 三个场景的结果对比

| 场景 | P007 | `source` | HTTP |
|---|---|---|---|
| A. MCP 关 | ✅ 出现（库存 2000） | `fallback` | 200 |
| B. MCP 开 | ❌ 消失（库存 0） | `mcp` | 200 |
| C. MCP 坏 | ✅ 出现（回落 2000） | `fallback` | 200 |

**这就是"降级"的具体样子**：B 是正常，C 坏了但没崩，用户仍然拿得到推荐（只是包含了实际没货的 P007）。

## 2.4 那 Host 呢？

**Host 在本项目里还没做**，它就是计划里的「运营 Copilot」。

区别在哪：

| | 本项目现在的用法 | Copilot 会做什么 |
|---|---|---|
| 谁决定调哪个工具 | **代码写死** —— 库存 Agent 就是调 `batch_query_stock` | **模型自己决定** —— 你问"哪些商品快断货了"，它自己选 `list_low_stock` |
| 调几次 | 固定 1 次 | 不确定，模型可能连调 3 个工具 |
| 谁负责收尾 | 代码 | 模型（循环直到它能回答） |

这就是为什么 **Copilot 才是真正的 harness 活** —— Server 只是包一层协议壳，Host 要**自己实现那个循环**。

## 2.5 为什么要故意让两份数据不一致

因为**否则你无法证明 MCP 真的生效了**。

如果 WMS 库存恰好等于 `Product.stock`，那么：

- MCP 开着 → 得到库存 2000
- MCP 坏了 → 回落得到库存 2000

**两种情况输出完全一样。** 测试全绿，但你其实什么都没验证到 —— 万一是 MCP 根本没走通、一直在降级呢？你分不出来。

现在有了差异，就有了**可验证的证据**：

```
MCP 开 → 15 个商品里 13 个可用
MCP 关 → 15 个商品里 15 个可用
唯一差异：P007 和 P014 —— 恰好是真数据里没货的那两个
```

> 这条在面试里叫「**契约测试**」：不是测"没报错"，是测"**行为真的变了，而且变在我预期的位置上**"。

---

# 第三部分：为什么要这么麻烦 —— 删掉会怎样

每一块都回答同一个问题：**删掉它，具体会损失什么？**

## 3.1 `asyncio.wait_for` 超时

**删掉会怎样**：
[settings.py](../python/config/settings.py) 里那些 `agent_timeout_*` 变成纯摆设。
实测 `user_profile` 要跑 8.8 秒，配置写的是 5 秒，**没人拦它**。
如果模型卡 60 秒，用户就真的等 60 秒。

**所以它换来的是**：一个**能被信任的配置项**。你写在配置里的数字，就是真实生效的数字。

> 面试可讲：**"我打开超时之前先把四个超时值按实测 P95 重标定了一遍 ——
> 因为原配置的 `user_profile` 是 5 秒，而实测要 8.8 秒，直接打开会让它每次都降级。"**

## 3.2 熔断器

**删掉会怎样**：
一个 Agent 挂了之后，**后续每一次请求都还会去试它** —— 白花 1.2 秒，然后失败。
有熔断的话，第 6 次开始 **0ms 直接跳过**。

**实测对比**（注入 0.3 秒超时，连打 4 次）：

```
第 1 次：311ms 降级   ← 超时生效
第 2 次：311ms 降级
第 3 次：  0ms 返回   ← 熔断打开，根本不打了
第 4 次：  0ms 返回
```

**所以它换来的是**：一个坏掉的依赖**不会持续拖慢整条链路**。

## 3.3 `request_id` 贯穿

**删掉会怎样**：
出问题时你只知道"某个 Agent 慢了"，**不知道是哪一次请求的**。
并发 10 个请求时，日志会完全糊在一起。

**所以它换来的是**：从"日志里捞针"变成"按 ID 查一次"。

## 3.4 `agent.retry` 事件

**删掉会怎样**：你**分不清"模型慢"和"在重试"**。

> 这条我有亲身教训。我一开始看到一次请求花了 63 秒，**判断"肯定是在重试"**。
> 后来加了这个事件，打了几次请求 —— **一次都没触发**。
> 真相是：那不是重试，是单次调用真的花了 54 秒。
> **没有这个事件，我会带着一个错误结论继续往下做。**

**所以它换来的是**：**先纠正你自己**。可观测性最大的价值往往不是发现别人的 bug，是发现你判断错了。

## 3.5 批量查库存（`batch_query_stock`）

**删掉会怎样**：改成逐个商品查。

实测**每次 MCP 调用的成本是约 1.15 秒**（每次都要重启一个子进程，光 `import mcp` 就占 1 秒）。
一次推荐要查最多 30 个商品：

```
逐个查：30 × 1.15 秒 ≈ 34.5 秒    ← 比整条链路其他部分加起来还慢
批量查：        1.15 秒
```

**所以它换来的是**：**省 33 秒**。

> 面试可讲：**"这个接口不是我拍脑袋加的，是实测出单次 MCP 调用要 1.15 秒之后，
> 由调用模式倒推出来的。"**

## 3.6 WMS 真实库存（自建 Server）

**删掉会怎样**：
推荐里会持续出现**实际没货的商品**。P007 在代码里写着"库存 2000"，那是编的。

**所以它换来的是**：库存判断有了**依据**，而且这个依据**可被验证**（见 2.5 的契约测试）。

## 3.7 MCP 降级（`source` 标记 + 回落）

**删掉会怎样**：
库存服务一挂，**整条推荐链路跟着挂**。用户拿不到任何推荐。
或者更糟：**静默降级** —— 系统悄悄用回假数据，你在监控上看到"一切正常"，
但推荐质量已经退化了，而你**不知道什么时候开始的**。

**所以它换来的是**：一个外挂依赖**不会成为单点故障**，而且降级**看得见**（`source` 字段）。

> 这条对应验收项 A4，是整个 MCP 方案里最值得讲的一条：
> **"我不是『接了 MCP』，我是『接了 MCP 并给它设了降级路径，还用故障注入验证过』。"**

## 3.8 `harness/deps.py`（组合根）

**删掉会怎样**：
进程里会有**两套互不相知的 Agent 实例**（`supervisor.py` 一套、`graph.py` 一套）。

具体后果：

- 一个端点把某 Agent 打到熔断，**另一个端点完全不知情**，继续往注定失败的下游上打
- `POST /api/v1/experiments/{id}/outcome` 记录的实验结论，
  **永远传不到** `/recommend/graph` 的 Thompson 采样

> 这两个都是**真实存在过的 bug**，不是假想的。它们不会让任何测试变红，
> 只会让线上行为难以解释 —— 所以专门写了测试锁住。

**所以它换来的是**：行为**可预测**。

## 3.9 一页汇总

| 删掉什么 | 具体损失 | 一句话价值 |
|---|---|---|
| 超时 | 配置里的 5 秒是假的，8.8 秒也拦不住 | 配置可信 |
| 熔断 | 坏掉的 Agent 每次仍白花 1.2 秒 | 坏依赖不拖垮链路 |
| `request_id` | 只能看到"某 Agent 慢"，不知是哪次请求 | 能查 |
| `agent.retry` 事件 | 分不清"慢"和"重试"（**我在此判断错过**） | 先纠正自己 |
| 批量查库存 | 20 个商品 ≈ 23 秒 | 省 33 秒 |
| WMS 真数据 | 推荐里出现没货的商品 | 判断有依据且可验证 |
| MCP 降级 | 库存一挂整链挂；或静默退化 | 不成为单点故障 |
| `deps.py` | 熔断与实验结论跨路径不同步 | 行为可预测 |

---

# 附录：面试前速查

> 这一节是**面试前一晚翻的**，不是现在读的。

## 一句话版本

> **harness** = 让 LLM 应用能在真实世界里跑起来的那层代码。
> 它不改变业务逻辑，包在业务逻辑外面，回答的全是"如果……怎么办"。

> **MCP** = AI 工具的 USB-C 接口。三个角色：
> Server（提供）、Client（使用）、Host（让模型自己决定用什么）。

## 面试问答

**Q：你的 harness 具体做了什么？**

A：四件事。
① **超时**——原来 `self.timeout` 是死字段，赋值了没人读，我接上 `asyncio.wait_for`；
② **熔断**——滑动窗口按 agent 名索引，连续 5 次失败打开，冷却 30 秒后半开探测一次；
③ **可观测**——`request_id` 用 structlog contextvars 贯穿到所有 Agent 日志，
一行没改业务代码；还补了 `agent.retry` 事件，因为原来一次重试完全不可见；
④ **降级**——两级，Agent 级回落 + 管线级兜底。

**Q：为什么要自己写熔断器，不用现成库？**

A：因为这个进程里有两个编排器（手写 asyncio 的 supervisor 和 LangGraph 的 graph），
它们各自持有 Agent 实例。第三方库的熔断器通常是实例级的 ——
那样同一个逻辑 Agent 会有两份互不相知的健康状态，一个端点打挂了另一个不知道。
我需要**按 agent 名索引的共享状态**，所以自己写了 ~200 行。顺带它也是 100% 离线可单测的。

**Q：你怎么证明这些真的生效了？**

A：故障注入。
超时：把 `agent_timeout_user_profile` 注入成 0.3 秒（正常 0.9 秒），
连打 4 次请求 —— 前两次 311ms 降级，第 3、4 次 0ms 短路（熔断打开），
**四次全部 HTTP 200，商品文案照常返回**。
MCP：把数据库路径指向不存在的文件 —— 仍然 200，响应里 `source` 从 `mcp` 变成 `fallback`。

**Q：你怎么知道 MCP 真的在起作用，而不是一直在降级？**

A：契约测试。种子数据**故意**和本地假数据不一致（P007 本地写 2000，WMS 里是 0）。
然后对比：MCP 开 13/15 可用，关 15/15 可用，**唯一差异恰好是 WMS 里没货的那两个**。
如果两者数据一致，这个测试就没意义 —— 通与不通输出相同。

**Q：这个项目最难的地方是什么？**

A：不是一个功能，是一次**判断失误的纠正**。
我一度认为某次 63 秒的请求是重试造成的。为了验证，我加了 `agent.retry` 事件 ——
结果一次都没触发。真相是模型单次调用真的花了 54 秒。
顺着这条线查下去，发现延迟和推理 token 数的相关系数是 **0.997**，
而 `max_tokens` 参数被模型厂商完全忽略（设成 64 反而生成了 2797 个 token）。
关掉推理后，全链路 p50 从 **48015ms 降到 4597ms**。

## 可能被追问的三个点

| 追问 | 怎么答 |
|---|---|
| "这是生产环境跑的吗？" | 诚实说：本地实测，没有真实流量。但**每个数字都来自实测，可复现**，报告在 `eval_results/` |
| "为什么不用 OpenTelemetry？" | 单进程单接口，`merge_contextvars` 已经在 structlog 默认链里，成本近乎零。OTel 要装 exporter + Collector，换不来 `grep request_id` 给不了的东西。**接缝留了**：`span()` 的形状是按 OTel span 设计的 |
| "A/B 实验有效果吗？" | **诚实说：目前实验不影响行为** —— `assign_thompson` 从没被调用，返回的 `config` 也没人读。这是个已知缺口，不是"CTR 提升 15%"那种编的数字 |

---

# 最后：现在这个项目处于什么状态

| | 之前 | 现在 |
|---|---|---|
| 全链路 p50 | 48,015 ms | **4,597 ms** |
| 超时 | 死字段 | 真生效，且按实测重标定 |
| 熔断 | 不存在 | 滑动窗口 + 半开，故障注入验证 |
| 日志 | 生成了 request_id 就扔 | 贯穿全部 Agent |
| 库存数据 | 代码里写死的假数据 | 通过 MCP 查真实 SQLite |
| MCP 挂了 | （无从谈起） | 降级 + 可观测，主接口仍 200 |
| 测试 | 5 个 | **78 个** |

**一句话**：原来是个"能跑但一碰就碎"的 demo，
现在是个**"我知道它会怎么碎、并且碎了我也不怕"** 的系统。
