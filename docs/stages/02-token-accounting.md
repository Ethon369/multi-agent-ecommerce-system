# 阶段 B：token 与成本账本

> 上一阶段：[01-fix-candidate-set.md](01-fix-candidate-set.md) · 下一阶段：03-tool-registry.md
>
> 一句话：**让"一次推荐花多少钱"从"不知道"变成"知道，而且能分摊到每个 Agent"。**

---

## 一、为什么需要这个

LLM 应用的成本是**隐性**的。你调用数据库能感觉到"这次查询花了多少"，但调用 LLM 不会 ——
它就是从你的余额里悄悄扣掉，一次推荐调 3~5 次，每次都吞掉几千个 token。

不记账的话，这三个问题全都答不上来：

| 问题 | 为什么重要 |
|---|---|
| 哪个 Agent 最贵？ | 优化要有方向，不能凭感觉 |
| prompt 缓存命中了吗？ | **命中价是未命中的 1/50**（实测差价），这是一个几乎免费的杠杆 |
| 这次改动让成本涨了没？ | 没有前后对比，就不知道自己是优化了还是劣化了 |

## 二、小白版原理：为什么"价格"和"token"要分开

这里有一条贯穿整个模块的原则：

> **token 是事实，价格是配置。**

- **token 数量**是从 API 响应里读出来的，是客观事实，不会变
- **价格**会变：厂商调价、搞促销、你换了模型、你切到另一个 provider

如果把价格硬编码在计算成本的函数里，那厂商一调价你的代码就错了，而且是**悄悄错**——
它还会算出一个看起来很精确的数字。

所以本模块的做法是：

```
token 数  ──┐
            ├──> 价格表（可被环境变量 / JSON 文件覆盖） ──> 成本
模型名    ──┘
                              │
                              └─ 查不到价格？返回 None
```

**关键决定：查不到价格时返回 `None`，绝不猜。**

响应里会显示 `"cost_usd": null, "cost_known": false`。

> 为什么这个"不猜"很重要：**一个看起来精确但其实是编的成本数字，比没有数字更危险** ——
> 你会拿它做决策（"这个方案太贵了，换掉"），而它可能差了十倍。

## 三、代码怎么实现的

### `python/harness/pricing.py` — 价格表

**面试考点**: 配置与代码分离、前缀匹配、查不到时的降级语义

```python
@dataclass(frozen=True)
class ModelPrice:
    """单位：美元 / 每百万 token。"""
    input_per_1m: float
    output_per_1m: float
    cached_input_per_1m: float = 0.0

    def cost(self, input_tokens, output_tokens, cached_tokens=0) -> float:
        # 缓存命中的输入 token 要【单独计价】—— 实测差价高达 50 倍
        # （deepseek-flash 命中 $0.003 vs 未命中 $0.15 每百万），
        # 只用一个 input 单价会把成本算得严重偏高。
        uncached = max(0, input_tokens - cached_tokens)
        return (uncached / 1_000_000 * self.input_per_1m
                + cached_tokens / 1_000_000 * self.cached_input_per_1m
                + output_tokens / 1_000_000 * self.output_per_1m)


class PricingTable:
    def lookup(self, model: str) -> ModelPrice | None:
        if model in self._prices:
            return self._prices[model]
        # 为什么用【前缀匹配】：模型名常带日期或版本后缀
        # （deepseek-flash / deepseek-flash-2026-08），精确匹配会导致
        # "明明配了价格却查不到"，然后静默变成 None。
        # 取最长前缀，避免 "deepseek" 这种短前缀抢走更精确的匹配。
        matches = [name for name in self._prices if model.startswith(name)]
        return self._prices[max(matches, key=len)] if matches else None
```

**两个设计取舍**（面试可能追问）：

| 取舍 | 理由 |
|---|---|
| 取**峰值价**（最贵档），不取均价 | 估算成本时**宁可高估** —— 高估让你更谨慎，低估让你超预算 |
| 提供 `fingerprint()` 指纹 | 对比"优化前 vs 优化后"的成本时，如果两次用的价格表不同，**那个对比就是假的**。指纹让这件事可验证 |

### `python/harness/usage.py` — 账本

**面试考点**: contextvars 与 asyncio.gather 的交互、可变对象 vs 值、并发记账的正确性

```python
@dataclass
class UsageAccumulator:
    by_agent: dict[str, AgentUsage] = field(default_factory=dict)
    by_model: dict[str, AgentUsage] = field(default_factory=dict)
    llm_calls: int = 0
    estimated: bool = False    # 至少一次用量是估算的（API 没返回 usage）

    def record(self, agent_name, model, usage_metadata, *, estimated=False) -> None:
        # ⚠️ 调用方必须保证：从进入本方法到离开，中间【没有 await】
        ...
        # reason 里的 output_token_details.reasoning 是 output_tokens 的【子集】，
        # 所以它只用于观测，不重复计入成本 —— 加两遍会让成本翻倍。
```

### `python/harness/llm.py` — 会记账的模型

**面试考点**: 包装模型 vs 包装调用点、pydantic 字段限制、对 bind_tools 的兼容

```python
class MeteredChatOpenAI(ChatOpenAI):
    agent_name: str = "unknown"    # 让模型自己知道"谁在调我"

    def _meter(self, result: ChatResult, messages) -> None:
        message = result.generations[0].message
        usage = getattr(message, "usage_metadata", None)
        if usage:
            record_usage(self.agent_name, self.model_name, usage)
            return
        # API 没返回 usage（部分 OpenAI 兼容网关会省略）时，
        # 用 tiktoken 估算并标记 estimated=True ——
        # 让下游知道这个数字是估的，不能当实测引用。
        ...

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        result = await super()._agenerate(...)
        self._meter(result, messages)
        return result
```

**为什么包装模型，而不是在每个调用点记账**：

三个 Agent 里各有一处 `llm.ainvoke(...)`。如果三处都手写记账，
将来加第 4 个 Agent 时**一定会漏** —— 而且漏了不会有任何报错，只是成本数字悄悄少一块。

包装模型之后，"记账"变成模型自带的行为：**只要 Agent 从 `build_chat_model()` 拿客户端，就自动被记账。**

**实现上的一个坑**：`ChatOpenAI` 是 pydantic 模型，不能把一个有状态的累加器当成字段塞进来
（pydantic 会做校验/序列化，而且同一个模型实例可能被多个请求复用）。
所以累加器放在 contextvar 里，记账时按**调用时刻**去取当前请求的账本。

**顺带的好处**：`bind_tools()` 返回的是 `RunnableBinding`，但它底层仍会走到本类的 `_agenerate` ——
所以将来 Copilot 用工具调用时，记账照样生效。

### `python/models/schemas.py` — 报告放哪

**面试考点**: pydantic 按声明类型序列化的陷阱

```python
class RecommendationResponse(BaseModel):
    # ⚠️ 这里声明的是基类 AgentResult，pydantic 会按【声明类型】序列化，
    # 所以子类独有字段会被静默截断，HTTP 响应里看不到。
    agent_results: dict[str, AgentResult] = Field(default_factory=dict)

    # 所以新信息要加到【顶层】：
    harness: HarnessReport | None = None
```

## 四、最容易写错的地方：并发记账

编排器用 `asyncio.gather` 让两个 Agent **同时**跑，它们会同时往账本里记数。
这里有三层容易写错的地方，**任何一层错了都会静默丢 token**：

| # | 陷阱 | 后果 |
|---|---|---|
| ① | 账本必须在 `gather` **之前**放进 contextvar | asyncio 任务在**创建时**复制上下文，晚了子任务拿到 `None` |
| ② | 放进去的必须是**可变对象**，不是值 | `ContextVar.set()` 在子任务里**不会**传播回父任务 |
| ③ | 读写之间不能有 `await` | 交错会让计数丢失 |

### 为什么 ② 是这样

```
父任务：acc = UsageAccumulator()
        _current.set(acc)          ← contextvar 里存的是【引用】
             │
             ├── 子任务 A（复制上下文，拿到同一个 acc 引用）
             │      acc.add(...)     ← 改的是对象属性 → 父任务看得见 ✓
             │
             └── 子任务 B（同上）
```

如果子任务里写 `_current.set(新账本)`，那只是**子任务自己的上下文**指向了新对象，
父任务完全不知道。所以正确写法是**改对象属性**，而不是**换对象**。

### 为什么 ③ 加锁没用

失败模式是"跨 `await` 的交错"，不是真并行（事件循环是单线程的）。
加 `asyncio.Lock` 拦不住这个问题，真正要做的是**不在读写之间让出控制权**。

> 这一组我写了 5 条测试专门守着，包括一条**反面测试**
> （`test_child_cannot_set_a_new_ledger`）—— 它固化"子任务里 set 新账本影响不到父任务"
> 这个事实。如果哪天这条测试失败了，说明整个并发模型的前提不成立了。

## 五、实测结果：账本立刻指出了成本大头

一次请求（`num_items=5`）：

```
LLM 调用次数 : 3
输入 token   : 1111  (缓存命中 0)
输出 token   : 2785  (其中推理 2436)
成本         : $0.003675

按 Agent 分摊：
  user_profile     calls=1  in=  241  out=  101  reasoning=   0
  product_rec      calls=1  in=  594  out=   20  reasoning=   0
  marketing_copy   calls=1  in=  276  out= 2664  reasoning=2436   ← 大头
```

**`marketing_copy` 一个 Agent 占了全部输出 token 的 96%、成本的 89%。**

而这个 Agent 正是我在上一轮**故意保留推理**的那一个（理由是"创作型任务，思考可能换来更好的文案"）。

> 这就是记账的价值：**它把一个主观问题变成了一个可以量化的取舍。**
> 原来我只能说"我觉得文案需要推理"，现在可以说
> "文案占了 89% 的成本，其中 2436 个 token 是推理 —— 值不值，得看评测"。

### 另一个发现：prompt 缓存确实在工作

第二次测量时出现了 `cached_tokens: 640`（命中率约 29%）。
缓存命中的输入按 $0.006/百万计价，未命中按 $0.30 —— **差 50 倍**。

验算成本计算是否正确：

```
未缓存输入 2234-640 = 1594  →  1594/1M × $0.30  = $0.000478
缓存命中    640             →   640/1M × $0.006 = $0.000004
输出       1384             →  1384/1M × $1.20  = $0.001661
                                        合计     = $0.002143  ✓ 与实际输出一致
```

## 六、怎么验证的

### 1. 单元测试（17 条，全部离线）

`python/tests/test_usage.py`

| 类别 | 守住的 | 代表测试 |
|---|---|---|
| 价格表 | 前缀匹配、最长优先、未知返回 None、缓存分开计价、指纹稳定 | `test_unknown_model_returns_none_never_guesses` |
| 账本 | 累加、reasoning 不重复计费、usage 缺失容错、估算标记粘性 | `test_reasoning_tokens_do_not_double_count_cost` |
| 作用域 | 装入/复位、无账本时静默忽略 | `test_scope_installs_and_restores` |
| **并发** | gather 共享账本、50 并发不丢更新、**子任务不能换账本**、两个请求不串账 | `test_two_concurrent_requests_do_not_mix` |

### 2. 端到端

`POST /api/v1/recommend` 的响应里出现 `harness.usage` 段；
`GET /api/v1/metrics` 的 `llm` 段给出进程启动以来的累计值。

`pytest tests/ -q` → **97 passed**（阶段 A 之后是 80，本阶段新增 17）

## 七、这一步在简历上怎么写

> 为多 Agent 系统实现 token 与成本账本：包装 ChatOpenAI 使记账成为模型自带行为
> （新增 Agent 不可能漏记），基于 contextvars 实现并发安全的按 Agent / 按模型分摊，
> 并区分缓存命中与未命中的差异化计价。
> 实测定位出营销文案 Agent 占 89% 的成本、其中 2436 个 token 为推理开销。

**为什么这条值得写**：
① 它体现的是**成本意识**（工程岗很看重，很多候选人只会说"我用了什么模型"）；
② 并发那段涉及 contextvars 与 asyncio 任务复制的交互，**是能讲深的细节**；
③ "包装模型而不是包装调用点"是一个**可复用的设计判断**，不是背书。

---

## 附：为什么价格表里是这两个数字

价格来自 [DeepSeek 官方 API 文档](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)（2026-09-20 查证）：

| 模型 | 输入（未命中） | 输入（命中） | 输出 |
|---|---|---|---|
| `deepseek-flash` | $0.30 / 百万 | $0.006 / 百万 | $1.20 / 百万 |
| `deepseek-v4-pro` | $1.32 / 百万 | $0.044 / 百万 | $3.96 / 百万 |

取的是**峰值时段价**（off-peak 是峰值的一半）。价格表里记了 `PRICING_AS_OF` 和来源链接，
`/api/v1/metrics` 里也会返回 —— 这样**一眼能看出这个表有多旧**。

换 provider 时用 `ECOM_PRICING_JSON` 覆盖即可，不用改代码。
