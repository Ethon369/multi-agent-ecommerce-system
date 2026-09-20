# 阶段 E：运营 Copilot —— MCP Host 端的多轮工具调用循环

> 上一阶段：[04-mcp-server-side.md](04-mcp-server-side.md) · 下一阶段：06-eval.md
>
> 一句话：**前四个阶段都是"代码写死调什么工具"，这一步要"让模型自己决定"。**

---

## 一、这一步为什么最稀缺

MCP 有三个角色，但大多数人只做第一个：

| 角色 | 干什么 | 本项目 |
|---|---|---|
| **Server** | 把能力挂出去 | ✅ recommend_server / wms_server |
| **Client** | 按名字调外部工具 | ✅ 库存 Agent 调 WMS |
| **Host** | **拿着模型，让它自己决定调哪些工具** | ← 本阶段 |

**Server 只是包一层协议壳。Host 要自己实现：循环边界、消息回灌、
工具失败的处理、迭代上限、强制收尾。** 这才是真正的 harness 工作。

## 二、小白版原理：什么叫"模型自己决定"

前面四个阶段里的工具调用长这样：

```python
# 代码写死的：先查库存，再过滤
stocks = await self.db.batch_query_stock(product_ids)
```

**调什么、什么时候调、调几次，全是代码决定的**，模型不参与。

Copilot 反过来：

```python
# 代码不知道要调几次、调什么 —— 由模型看着工具列表自己决定
循环：
    模型说"我要调 A"  -> 执行 A -> 把结果塞回对话
    模型说"我还要调 B" -> 执行 B -> 把结果塞回对话
    模型说"我知道了，答案是……" -> 结束
```

**实测里它就自己做了这个推理**（问"哪些商品快断货了"）：

```
step1  wms__list_low_stock   1150ms   ← 它自己决定先调 MCP 查库存
step2  list_products            0ms   ← 然后自己决定再调本地目录拿商品名
```

MCP 只返回商品 ID 和库存数字，但对运营人员来说没有商品名就没用 ——
**模型自己意识到这一点，又调了第二个工具**。这不是代码安排的。

## 三、为什么【不】继承 BaseAgent

这是本阶段最重要的一个设计判断。

`BaseAgent` 的契约是"整体重试 + 返回 `confidence=0` 的降级结果"。
**对多轮工具调用循环来说这两条都不对**：

| # | 问题 |
|---|---|
| ① | **整体重试会重放工具调用**。当前工具都是只读的还好，一旦将来加了写工具就会重复执行 |
| ② | 每次重试都要把前面几轮的 token **再烧一遍** |
| ③ | `confidence=0` 的降级结果对一次聊天来说是**错的形状** —— 用户要的是一段话，不是一个降级标记 |

所以 `CopilotAgent` **独立实现**，只复用 harness 的**零件**：
工具注册表、trace、账本、以及注册表内建的单次调用级重试。

> **面试可讲**："我为什么没有复用那个基类" —— 这比"我复用了一个基类"更能说明
> 你理解每个抽象的适用边界在哪。**知道什么时候不该用，比会用更难。**

## 四、代码怎么实现的

### 三重护栏（绝不用 `while True`）

```python
for step_no in range(1, self.max_steps + 1):          # ① 迭代次数上限
    if time.monotonic() > deadline:                   # ② 墙钟时间上限
        stop_reason = "budget_time"; break
    if usage.input_tokens + usage.output_tokens > self.max_tokens:   # ③ token 预算
        stop_reason = "budget_tokens"; break

    ai = await bound.ainvoke(messages)
    messages.append(ai)

    if not ai.tool_calls:
        stop_reason = "completed"; reply_text = ai.content; break   # 模型自己收尾

    for tc in ai.tool_calls[:MAX_PARALLEL_CALLS]:     # 单轮最多并行 4 个
        res = await self.registry.call(tc["name"], **tc["args"])
        messages.append(ToolMessage(...))             # 结果回灌
```

**三条护栏相互独立** —— 任何一条触顶都能让循环停下来。
`MAX_PARALLEL_CALLS` 是第四条：防止模型一口气点 20 个工具把预算吃光。

### 触顶之后强制收尾

```python
if stop_reason != "completed":
    final = await self.llm.bind(tool_choice="none").ainvoke(
        [*messages, HumanMessage(content="请基于以上已有信息直接作答，不要再调用工具。")]
    )
```

**为什么需要这一步**：循环触顶时，最后一条消息可能是一个**悬空的工具调用**
（模型想调工具，但我们已经不给它机会了）。直接返回的话用户拿不到任何回答。

`tool_choice="none"` 明确告诉模型"这一轮不许调工具，只能说话"。

> **用户永远能拿到一段文字，而不是一个悬空的工具调用。**

### 工具失败【回灌】而不是中断

```python
payload = (
    res.value if res.ok
    else {"error": res.error, "degraded": res.degraded,
          "hint": "该工具本次不可用，请基于其他已有信息作答或换一个工具。"}
)
messages.append(ToolMessage(content=json.dumps(payload, ...), tool_call_id=tc["id"]))
```

**不 raise、不 break** —— 让模型自己决定怎么办：换个工具查，还是基于已有信息作答。

**这正是"工具挂了仍然能给出回答"的原因。** 实测里模型幻觉出一个不存在的工具名时，
循环也没有崩，它自己换了个说法继续回答。

### 只把只读工具给模型

```python
self.allowed_tags = allowed_tags if allowed_tags is not None else {"read_only"}
```

写操作不该由聊天触发 —— 即便 `record_experiment_outcome` 是幂等安全的。
有一条测试专门守着这件事（`test_only_read_only_tools_are_exposed`）。

### 温度必须调低

```python
self.llm = build_chat_model("copilot", temperature=0.2, max_tokens=1024)
```

**工具调用对温度很敏感**：温度高了模型会时而输出工具调用、时而输出一段文字，
行为不稳定。营销文案那个 Agent 用 0.9 是对的（要创造性），
Copilot 用 0.2 也是对的（要可靠）。

## 五、实测结果

### 场景一：MCP 开着

**问**：「哪些商品快断货了？给我列一下」

```
stop_reason = completed     steps = 2
step1  wms__list_low_stock   1150ms   ok=True
step2  list_products            0ms   ok=True

回答：以下是当前库存告急的商品清单（共 5 款，均为紧急补货级别）：
| 商品 | 库存 | 状态 | 建议动作 |
| Anker 140W充电器（P007） | 0 | 🔴 已断货 | 紧急补货 |
| 大疆Mini 4 Pro（P014）   | 0 | 🔴 已断货 | 紧急补货 |
| Switch 2（P015）         | 5 | 🔴 临界   | 紧急补货 |
| iPad Air M3（P005）      | 40| 🔴 临界   | 紧急补货 |
| 罗技MX Master 3S（P010） | 50| 🔴 临界   | 紧急补货 |
```

**注意它是自己决定调两个工具的**：MCP 只给 ID 和库存，但要有商品名才有用。

### 场景二：MCP 关着（只有 3 个内置工具）

**问**：同样的问题

```
stop_reason = completed     工具调用 = ['list_products']

回答：我拿到了商品目录，但这里只有静态信息（名称、类目、价格、标签），
     **不含实时库存数据**。我目前可用的工具里没有库存查询功能，
     所以无法判断哪些商品快断货了。……
     建议：- 如果你那边有库存查询的入口，麻烦确认一下是否已接入给我；
```

**它没有编造库存数字。** 而且这是**工具描述在起作用** ——
`list_products` 的描述里明确写了「这是静态目录，不含实时库存 ——
要查某个商品还有多少货，请用库存查询工具」，模型正确地用了这句话。

> 这就是为什么工具描述要写得具体：**含糊的描述会让模型乱调或者不调。**

### 场景三：数据异常时

**问**：「系统现在健康吗？今天花了多少钱？」（当时指标确实是空的）

```
回答：我查了系统运行指标，但结果有点异常，需要跟你说明一下：
     - 各 Agent 的运行指标：空（没有任何 Agent 的调用记录）
     - 今日成本：$0.00
     怎么理解这个结果：有两种可能，需要你判断一下：
     1. 系统今天确实没有产生任何调用……
     2. 指标采集可能没正常工作——因为连一个 Agent 的记录都没有，这不太像正常
```

**它没有给出"一切正常"的虚假结论，而是如实报告异常并列出可能原因。**
这正是运营助手该有的行为。

## 六、怎么验证的

`python/tests/test_copilot.py`，17 条，全部用**假模型**驱动。

**为什么必须用假模型**：这里要测的是**循环的控制逻辑**（什么时候继续、
什么时候停、触顶了怎么办），这些用真模型没法稳定复现。
假模型按脚本返回，每条控制分支都能被精确命中。

| 类别 | 守住的 | 代表测试 |
|---|---|---|
| 正常路径 | 模型不再调工具时结束、结果回灌、单轮多工具 | `test_tool_results_are_fed_back_to_model` |
| **护栏①** | 迭代上限触发强制收尾、收尾失败也有兜底文案 | `test_max_steps_triggers_forced_synthesis` |
| **护栏③** | token 预算触顶 | `test_token_budget_stops_the_loop` |
| **工具失败** | 失败回灌而非中断、幻觉工具名不崩 | `test_tool_failure_is_fed_back_not_raised` |
| 只读约束 | 写工具不暴露给模型 | `test_only_read_only_tools_are_exposed` |
| 会话 | 历史保留、会话隔离、reset 生效 | `test_sessions_are_isolated` |
| 边界 | 无工具、模型调不通、收尾为空 | `test_llm_failure_is_reported_not_raised` |

**写这些测试时发现的一个问题值得记下来**：
我原先写了一条"参数是非法 JSON 时兜住"的测试，结果构造不出来 ——
因为 `AIMessage` 是 pydantic 模型，`tool_calls[*].args` 类型就是 `dict`，
传字符串直接 `ValidationError`。

**于是循环里那段 `json.loads(args)` 的防御分支是死代码，删掉了。**
改成一条契约测试锁住"langchain 保证 args 是 dict"这个前提 ——
如果哪天框架放宽了约束，测试会失败，提醒我们把防御加回来。

`pytest tests/ -q` → **143 passed, 1 skipped**（阶段 D 之后 126+1，本阶段新增 17）

## 七、这一步在简历上怎么写

> 实现运营 Copilot（MCP Host 角色）：基于自建工具注册表实现多轮 tool-calling loop，
> 由模型自主决定调用哪些内置工具与 MCP 工具。设计三重独立护栏
> （迭代上限 / 墙钟预算 / token 预算）并实现触顶后的强制收尾，
> 保证用户始终得到回答而非悬空工具调用；工具失败以结果回灌而非中断循环。
> 刻意不复用项目的 BaseAgent 基类 —— 因其"整体重试"契约会重放工具调用并重复计费。

**为什么这条最值得写**：
① 它是 MCP 三端里**最少人做**的一端；
② 三重护栏 + 强制收尾是**能讲深的设计**（为什么不用 while True？为什么需要收尾？）；
③ "为什么不复用基类"这个判断，展示的是**理解抽象的适用边界**，
   比"我会用设计模式"有说服力得多。

---

## 附：文件与限制

| 文件 | 职责 |
|---|---|
| `copilot/agent.py` | 循环本体、三重护栏、强制收尾、会话 |
| `copilot/router.py` | `POST /api/v1/copilot` |

**已知限制（要诚实写出来）**：

| 限制 | 影响 | 修法 |
|---|---|---|
| 会话存在**进程内存** | 多 worker 部署会失效（第二次提问可能落到另一个进程） | 换 Redis（`services/feature_store.py` 已有 Redis 用法）或让客户端带历史 |
| 无鉴权 | 任何人都能调，而它会消耗 LLM 费用 | 加 API key 中间件 |
| 无流式输出 | 用户要等整个循环跑完才看到回答 | SSE |
