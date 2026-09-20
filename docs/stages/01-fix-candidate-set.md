# 阶段 A：修复"返回商品数少于请求数"

> 这是二次开发的第一阶段。选它当第一步的理由很简单：
> **它是正确性问题，不修掉的话，后面所有基于"返回 N 个商品"的测试和文档都建立在不成立的前提上。**
>
> 上一阶段：[progress.md](../progress.md) · 下一阶段：02-token-accounting.md

---

## 一、问题长什么样（先看现象）

请求里写 `"num_items": 5`（我要 5 个商品），实际返回 **2–3 个**，而且：

- **没有任何报错**
- **没有告警日志**
- HTTP 状态码还是 200，看起来一切正常

```
请求 5 个 -> 实得 2 个  ['P007', 'P010']
请求 5 个 -> 实得 2 个  ['P007', 'P010']    ← 两次一模一样，不像随机
请求 5 个 -> 实得 3 个  ['P007', 'P003', 'P010']
```

## 二、小白版原理：什么是"召回"

推荐系统不是从"全部商品"里直接挑，那样太慢。它的标准做法分两步：

| 步骤 | 干什么 | 打比方 |
|---|---|---|
| **召回**（recall） | 从海量商品里**快速粗筛**出几十个候选 | 招聘先从 10000 份简历里筛出 50 份 |
| **精排**（rerank） | 对这几十个**精细排序**，取前 N 个 | HR 面完 50 个人，挑最好的 5 个 |

关键点：**精排只能从召回出来的那几十个里挑。** 你不可能"精排"出一个没进候选的人。

## 三、这个 bug 的机制

你的项目里，同一个商品集合被**召回了两遍**：

```
Phase 1  ──> 召回 A 集合（10 个：P001~P010）
              │
              └─> 库存 Agent 检查的【就是这 10 个】

Phase 2  ──> 【又召回一次】得到 B 集合（15 个：P001~P015）
              │
              └─> 重排从 B 集合里挑了 5 个（可能含 P011~P015）

最后     ──> 过滤：只保留"库存检查过且有货"的
              │
              └─> P011~P015 根本不在检查过的范围里 -> 被刷掉
```

**用招聘打比方**：

- A 部门（Phase 1）筛了 10 份简历，体检了这 10 个人
- B 部门（Phase 2）**自己重新筛了一遍**，从 15 份里挑了 5 个人
- 最后要求"体检合格才能入职" —— 那 5 个人里有 3 个是 A 部门没体检过的，**直接刷掉**
- 结果只入职 2 个人，但**没人生气，因为流程"正常走完了"**

## 四、为什么这件事很隐蔽

代码里有一个兜底逻辑：

```python
final_products = [p for p in ranked_products if p.product_id in available_ids]
if not final_products:                    # ← 一个都没剩才触发
    final_products = ranked_products[:request.num_items]
```

它只在"**一个都没剩**"的时候才补位。而现在每次都剩 2-3 个，所以：

> **兜底不触发，系统认为一切正常，静默地少给了商品。**

这就是这类 bug 最典型的样子：**不是崩溃，是"看起来正常的错"**。

## 五、代码怎么改的

### `python/agents/product_rec_agent.py` — 商品推荐 Agent

**面试考点**: 召回与精排的集合一致性、可选的候选集注入、避免重复计算

```python
async def _execute(self, **kwargs: Any) -> ProductRecResult:
    user_profile = kwargs.get("user_profile")
    num_items: int = kwargs.get("num_items", 10)

    # 【新增】允许调用方把"已经召回、且已经被库存检查过"的候选集传进来。
    #
    # 为什么需要这个参数：
    #   编排器里 Phase 1 先召回一批，库存 Agent 检查的就是这一批；
    #   而 Phase 2 原先会【再召回一次】，两次集合可能不同 ——
    #   重排挑了 A 集合的商品，却被按 B 集合的检查结果过滤掉，
    #   最终返回数量静默少于 num_items。
    #
    # 修复思路：召回 -> 检查 -> 排序 -> 过滤，这四步必须作用在【同一个集合】上。
    provided: list[Product] | None = kwargs.get("candidates")
    candidates = provided if provided else await self._recall(user_profile, num_items * 3)
    rated_ids = await self._rerank(user_profile, candidates, num_items)
```

**两个设计取舍**（面试可能追问）：

| 取舍 | 为什么这么选 |
|---|---|
| 用**可选参数**而不是必填参数 | 单独调用这个 Agent 时（比如写单测）不用关心候选集，保持原行为 |
| `provided if provided else ...` 用真值判断 | Phase 1 失败时 `raw_products` 会是空列表。**空集合不能被当成"有效候选集"**，否则重排无米下锅、返回 0 个商品 |

### `python/orchestrator/supervisor.py` — 编排器

**面试考点**: 数据流一致性、并行编排中的依赖传递

```python
# Phase 2: 并行 —— 重排 + 库存检查
#
# 关键：把 Phase 1 召回的那批商品【原样传给重排】。
# 库存 Agent 检查的就是 raw_products，所以重排必须在同一个集合里挑，
# 否则重排挑中的商品不在检查过的集合里，会被下面的过滤【静默刷掉】。
# 顺带也去掉了一次多余的重复召回。
rerank_task = self.product_rec_agent.run(
    user_profile=user_profile,
    num_items=request.num_items,
    candidates=raw_products,        # ← 新增这一行
)
inventory_task = self.inventory_agent.run(products=raw_products)
```

### `python/orchestrator/graph.py` — LangGraph 版编排器

**面试考点**: 同一问题的两处实现要保持同步

```python
async def rerank_node(state: PipelineState) -> PipelineState:
    # 与 supervisor.py 同理：必须复用 Phase 1 召回、且库存已检查过的候选集。
    result = await get_agents()["product_rec"].run(
        user_profile=state.get("user_profile"),
        num_items=state.get("num_items", 10),
        candidates=state.get("raw_products", []),      # ← 新增
    )
```

> ⚠️ 这个项目有**两个做同一件事的编排器**（手写 asyncio 的 supervisor + LangGraph 的 graph）。
> 改一处必须改另一处，否则两条路径行为不一致。这也是阶段 C 要处理的问题之一。

**面试怎么说**：

> "这个 bug 是实测发现的：请求 5 个商品稳定只返回 2-3 个且完全静默。
> 定位后发现根因是流水线里**两处召回集合不一致** —— Phase 1 召回 10 个、
> 库存只检查这 10 个，而 Phase 2 又独立召回 15 个并从中排序，交集自然就少了。
>
> 修复的原则是**让四步作用在同一个集合上**：召回 → 检查 → 排序 → 过滤。
> 我选了'复用 Phase 1 的候选集'而不是'扩大库存检查范围'，
> 因为后者等于把召回这一步废掉 —— 召回的意义本来就是先缩小范围。
>
> 顺带也去掉了一次多余的重复召回。"

## 六、怎么验证的

### 1. 先证明测试真的能抓住这个 bug

写完测试后我做了一件容易被跳过的事：**把修复临时摘掉，确认测试会失败**。

```
# 摘掉 candidates=raw_products 之后：
supervisor.complete  product_count=2      ← 要 5 个，给 2 个，bug 复现
E  AssertionError: Phase 2 没收到 candidates —— 静默丢商品的 bug 会复现
```

**如果不做这一步，你写的测试可能是个摆设** —— 它可能恰好在有 bug 和没 bug 时都通过。

### 2. 端到端实测

```
修复前：要 5 个 -> 稳定 2-3 个
修复后 #1: 要 5 个 -> 5 个  ['P007','P006','P003','P010','P004']
修复后 #2: 要 5 个 -> 5 个  ['P007','P010','P006','P003','P004']
修复后 #3: 要 5 个 -> 5 个  ['P007','P006','P010','P003','P005']
修复后 #4: 要 5 个 -> 5 个  ['P007','P006','P010','P003','P004']
```

### 3. 全量测试
`pytest tests/ -q` → **80 passed**（修复前 75，新增 5 条）

## 七、新增的测试

`python/tests/test_rerank_candidates.py`

| 测试 | 守住什么 |
|---|---|
| `test_provided_candidates_skip_recall` | 传了候选集就不该再召回（这正是集合不一致的来源） |
| `test_without_candidates_still_recalls` | 没传时保持原行为，不影响单独调用 |
| `test_empty_candidates_falls_back_to_recall` | 空集合必须回落，不能被当成有效候选 |
| `test_reranked_products_are_subset_of_candidates` | **核心不变式**：重排结果必须是候选集的子集（连模型幻觉出的 ID 也要被丢弃） |
| `test_supervisor_returns_full_num_items` | 端到端：库存充足时请求 N 个就返回 N 个 |

其中最后一条的假 Agent **刻意复现了原 bug 的条件** —— 它让"自己召回"在第一次和第二次
返回**部分重叠**的两个集合。这一步很关键：如果两次召回完全不相交，
兜底逻辑会触发、反而掩盖问题；必须造出"剩几个但不是全部"这种最隐蔽的情况。

## 八、这一步在简历上怎么写

> 定位并修复推荐链路的**静默正确性缺陷**：Phase 1 与 Phase 2 的候选集不一致，
> 导致重排结果被按另一集合的库存检查结果过滤，返回数量长期少于请求量且无告警。
> 统一为"召回→检查→排序→过滤"四步作用于同一集合，并补充端到端的数量契约测试。

**为什么这条值得写**：它体现的不是"会写代码"，而是
①**能从"看起来正常"里发现异常**（2-3 个商品其实是个 bug）；
②**能定位到两个模块之间的数据流不一致**（这种 bug 单看任何一个文件都看不出来）；
③**修完之后回头验证测试真的有效**。
