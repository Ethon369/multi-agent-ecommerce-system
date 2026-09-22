# 二次开发进度总览

> 最后更新：2026-09-22
> 目标：投「AI 应用 / Agent 开发岗（校招）」，突出 **harness 工程能力** 与 **MCP 双侧**
> 原始计划：`docs/mcp-integration-prd.md`（MCP）、`docs/extension-guide.md`（二次开发指南）

**一句话现状**：**七个阶段全部完成**，并且 **P0/P1 工程缺口已清零**。
harness 运行时保障层、MCP 三端（Server + Client + Host）、
评测闭环、诚实化全部落地，每一项都有实测证据。

**最近一轮（2026-09-22 下午）**：**清 P0 / P1 缺口** —— 修掉 3 个 P0
（compose 路径失效、容器内跑 reload、5 条路由零测试）+ 4 个 P1
（structlog 从未配置、零引用依赖、无鉴权+CORS 通配、无全局异常处理器）。
测试 **169 → 208**。详见文末「本轮（2026-09-22 下午）：清 P0/P1」。

**上一轮（2026-09-22 上午）**：把 Redis 实时特征层从"假的"变成了"真的" ——
它原先**根本没接线**（全仓库没有一处 `FeatureStore(...)`），现在被**重写并接进**画像 Agent
（开关默认关闭）。详见文末「本轮（2026-09-22）」。

---

## 一、计划里程碑（M0–M8）

| # | 内容 | 状态 | 关键产出 / 证据 | 阶段文档 |
|---|---|---|---|---|
| **M0** | 基线固化 | ✅ | 基线 p50 = **48,015 ms**；`pytest` 进依赖；Java/Go 删除已提交 | — |
| **M0b** | tool-calling 探针 | ✅ | 模型支持原生 tool calling；**langchain-core 原生认 MCP schema，适配器代码量为 0** | — |
| **M1** | 请求级追踪 | ✅ | `request_id` 贯穿；`agent.retry` 事件上线（并据此推翻我一个错误结论） | — |
| **M1.5** | 评测运行器 | ✅ | 10 条 golden set（2026-09-22 增至 12 条，新增两条 Redis 用例）+ 确定性门禁 + `--baseline` 对比 | [06](stages/06-eval.md) |
| **M2** | 真超时 + 熔断 | ✅ | 故障注入 4/4 HTTP 200；熔断第 3 次请求 **0ms** 短路 | — |
| **M3** | token / 成本账本 | ✅ | 按 Agent 分摊；实测定位出文案占 89% 成本 | [02](stages/02-token-accounting.md) |
| **M4** | 工具层 + 组合根 | ✅ | `ToolSpec`/`ToolRegistry` + `deps.py` 组合根（修掉实例分裂 bug） | [03](stages/03-tool-registry.md) |
| **M5** | MCP 客户端侧 | ✅ | 见下方 D1–D10 | — |
| **M6** | 运营 Copilot | ✅ | MCP Host 端多轮 tool-calling loop + 三重护栏 | [05](stages/05-ops-copilot.md) |
| **M7** | 延迟取证 | ✅ | p50 48,015 → **2,522 ~ 2,788 ms**（约 17 倍）；成本 4.1 倍改善 | [06](stages/06-eval.md) |
| **M8** | 诚实化 | ✅ | README 顶部加「哪些数字能信」；清除全部未实测数字与已删除项 | [07](stages/07-honesty.md) |

图例：✅ 完成并验证 · ⚠️ 部分完成 · ❌ 未开始

**另外还做了一件计划里没有的事**：修掉了一个实测发现的静默正确性 bug
（返回商品数长期少于 `num_items`）—— 见 [阶段 01](stages/01-fix-candidate-set.md)。

---

## 二、MCP PRD 交付物与验收项

### 交付物（D1–D10）

| # | 交付物 | 状态 | 说明 |
|---|---|---|---|
| D1 | `mcp_servers/recommend_server.py` | ✅ | 2 个工具，协议壳零业务逻辑；返回扁平结构（信息在顶层，对模型更友好） |
| D2 | `mcp_servers/wms_server.py` | ✅ | 4 工具 + 1 resource，stdio |
| D3 | `mcp_servers/init_wms_db.py` | ✅ | 15 商品，**9 个与目录库存故意不同**（A5 的根据） |
| D4 | `services/mcp_client.py` | ✅ | 每次调用短连接，永不抛异常给调用方 |
| D5 | `agents/inventory_agent.py` 接线 | ✅ | 唯一接线点 |
| D6 | 配置开关 | ✅ | 4 个 `ECOM_MCP_*`，全部默认 `false` |
| D7 | `main.py` 生命周期管理 | ⚠️ **按设计不需要** | 选了短连接 ⇒ 进程内无常驻 MCP 连接可管。**这是我的判断，与原 PRD 有出入** |
| D8 | 测试 | ✅ | `tests/test_mcp_wms.py` + `tests/test_inventory_mcp.py` |
| D9 | `docs/mcp-integration.md` 使用文档 | ✅ | 怎么配 / 怎么验 / 常见问题 |
| D10 | 依赖 | ✅ | `mcp>=2.0,<3`（v2 稳定线，不装 `langchain-mcp-adapters`） |

### 验收项（A1–A7）

| # | 验收项 | 状态 | 证据 |
|---|---|---|---|
| A1 | `mcp dev` 可发现 Server | ⚠️ **未验证** | `mcp` 命令行存在，但缺 `typer`（需 `pip install mcp[cli]`）。
不过工具可发现性已由 `list_tools` 的进程内测试覆盖 |
| A2 | 工具逻辑（进程内 Client） | ✅ | 12 条测试，不起子进程不占端口 |
| A3 | 端到端 200 + 日志可见 MCP 调用 | ✅ | `inventory.stock_source source=mcp` |
| A4 | **降级：MCP 挂掉仍 200** | ✅ | `source="fallback"`，商品文案照常返回 |
| A5 | **契约：证明真的走了 MCP** | ✅ | 见下方「A5 证据」 |
| A6 | 回归探针 | ⚠️ **未做** | PRD 说探针归档在该目录，但里面**没有** `probe_recommend.py`。
替代：`eval/runner.py` 的 12 条用例已覆盖回归 |
| A7 | 开关关闭时零影响 | ✅ | 关时不构造客户端，行为与集成前一致 |

---

## 三、关键实测数字

所有数字均为实测，可复现（脚本在 `d:\tmp\`，结果在 `eval_results/`）。

### 延迟

分两步优化（每一步都有实测依据）：

| | 全链路 p50 | 说明 |
|---|---|---|
| 基线 | **48,015 ms** | 二次开发前的实测值 |
| 第一步后 | **4,597 ms** | 关掉确定型 Agent 的推理（10.4 倍） |
| **第二步后** | **2,522 ~ 2,788 ms** | 文案也关推理（累计约 17 倍） |

| Agent | 基线 | 第一步后 | 最终 |
|---|---|---|---|
| `user_profile` | 8,819 ms | 961 ms | ~700 ms |
| `product_rec` | 29,700 ms | 728 ms | ~500 ms |
| `marketing_copy` | 5,059 ms | 2,900 ms | **~1,200 ms** |

> 文案那一步起初是【保留推理】的（理由是创作型任务），阶段 F 用评测集检验后按证据改成了关闭 —— 见 [阶段 06](stages/06-eval.md)。

**怎么做到的**（完整诊断链，每步都有数据）：

1. 观察到同一 rerank 调用延迟 10.3s – 54.4s 波动
2. 我据此推断「在重试」→ 上线 `agent.retry` 事件 → **事件一次没触发，我的推断错了**
3. 重复 6 次：`input_tokens` 恒为 612、延迟与 reasoning token 相关系数 **r = 0.997**、98.5% 的 output 是推理 token
4. 逐个试关闭手段：

   | 手段 | 结果 |
   |---|---|
   | `max_tokens=64` | **反而更慢**（15554ms，生成 2797 token）—— 该参数被完全忽略 |
   | `reasoning_effort=low` | 无效 |
   | 提示词要求直接作答 | 有效但有限（-40%） |
   | **`thinking={"type":"disabled"}`** | **981ms（-89%），推理 token 归零** ← 采用 |

5. 验证质量没塌：JSON 合法、长度契约精确、ID 合法，且 `num_items=3` 时排序结果与开推理**完全一致**

### 故障注入（harness）

注入 `ECOM_AGENT_TIMEOUT_USER_PROFILE=0.3`（正常约 0.9s）：

```
#1 311ms  user_profile exceeded 0.3s budget   HTTP 200
#2 311ms  user_profile exceeded 0.3s budget   HTTP 200
#3   0ms  circuit_open: user_profile          HTTP 200   ← 熔断打开
#4   0ms  circuit_open: user_profile          HTTP 200
```

### 故障注入（MCP）

| 场景 | HTTP | `data.source` | 商品数 |
|---|---|---|---|
| MCP 正常 | 200 | `"mcp"` | 3 |
| MCP 坏掉 | **200** | **`"fallback"`** | 4 |

### 实时特征（Redis，2026-09-22）

前置：`scripts/seed_behavior.py --reset` 灌三个种子用户，端到端实测：

| 用户 | `data.source` | 画像给出的 segments | 说明 |
|---|---|---|---|
| `u_seed_active` | `redis` | `['active','high_value']` | 窗口内有数据；`活跃时段: 上午8-10点` 来自真实算出的 `active_hours` |
| `u_seed_churn` | `redis` | `['churn_risk']` | 45 天前来过、窗口内为空 —— 旧行为下他会被喂写死的"近 7 天浏览 25 次"，**永远出不了 `churn_risk`** |
| `u_seed_new` | `redis_empty` | `['new_user']` | 从没被上报过，走冷启动分支 |

降级实测（把 Redis 指向死端口）：

| 场景 | HTTP | `data.source` | confidence |
|---|---|---|---|
| Redis 正常 | 200 | `redis` | 0.95 |
| Redis 指向死端口 | **200（3/3）** | **`fallback`** | 0.7 |

评测：**12/12 通过**（含 rec_011 / rec_012 两条 Redis 用例；开关关闭时这两条自动跳过）。

> 边界：`redis_empty` 时**不**回落写死的兜底值。替用户编造行为不是降级，是造假 ——
> 一个其实已经流失的用户如果被喂上"近 7 天浏览 25 次"，模型不可能给出 churn_risk。

### A5 证据（去掉 LLM 变量，直接对比 `InventoryAgent`）

```
MCP 开  13/15 可用   source=mcp
MCP 关  15/15 可用   source=fallback
唯一差异：P007、P014 —— 恰好是 WMS 库存为 0 的两个（目录分别说 2000 / 100）
```

> 种子数据**故意**与 `Product.stock` 不一致就是为了这个：若两者相同，
> MCP 通与不通输出完全一致，测试全绿也说明不了任何事。

### 工程指标

- 测试：**5 → 144**（另有 1 条默认跳过的慢速完整链路）；2026-09-21 删掉 A/B 引擎及其 5 条测试后为 **139**；
  2026-09-22 接上 Redis 特征层后为 **168 passed, 1 skipped**；
  2026-09-22 下午补上接口级测试后为 **207 passed, 1 skipped**（共 208 条，新增 39 条）
- 接口级测试：**0 → 39**。新增 `tests/test_api.py`，覆盖全部 5 条路由 + 错误信封 + 三个中间件
- commit：10 个（每个都是独立可验证的单元）
- MCP 单次调用成本：**约 1,150 ms**（短连接重启子进程，`import mcp` 占约 1 秒）

---

## 四、与原计划 / 文档的偏离

这些是执行中发现事实与文档不符而做的调整，**每一条都有实测依据**。

| # | 原始说法 | 实测 | 处理 |
|---|---|---|---|
| 1 | PRD：短连接开销 100–300ms | **1,150 ms** | 据此把「必须批量查」的论证从「省 30 次往返」升级为「省 22 秒」 |
| 2 | PRD：改动压缩在 `_check_stock` 一个方法里 | 那是**逐商品**调用 = N × 1.15s ≈ 23 秒 | 改为 `_execute` 开头批量取一次 |
| 3 | PRD：v1→v2 是破坏性重写 | 已验证：`mcp.server.fastmcp` 已**删除**，v1 教程照抄直接 `ImportError` | 按 v2 写，未发现偏差 |
| 4 | 文档：`BaseAgent` 已内建 `asyncio.wait_for` 超时 | **不存在** | M2 补齐；这也是 PRD 降级设计能成立的前提 |
| 5 | 文档：熔断器 >50% 错误率触发 | 不存在；且 `error_rate` 是累计比率，单调不降 | 改为滑动窗口 + 计数触发 |
| 6 | 计划：`build_chat_model` 排在 M3 | M2 要在 5 倍方差的调用上标超时，不先压方差无法标 | 提前到 M2 之前 |
| 7 | 计划：`harness/deps.py` 排在 M4 | D1 和 Copilot 都要共享单例 | 提前到 M5 一并做 |

---

## 五、承重 vs 可删（审计）

用 `grep` 统计 harness 每个导出项在**生产代码**（排除定义处与测试）里的真实引用次数。
问题很直接：**这些代码是真的在承重，还是我写多了？**

| 模块 | 生产引用 | 判定 | 它换来的是什么 |
|---|---|---|---|
| `build_chat_model` | 6 | **承重** | 10.4 倍延迟优化的载体 |
| `get_agents` / `get_supervisor` / `get_metrics_collector` | 8 / 2 / 2 | **承重** | 修掉跨路径实例分裂（熔断状态各算各的） |
| `request_context` / `scope` / `bind` / `new_request_id` | 4 / 4 / 3 / 6 | **承重** | request_id 贯穿全部 Agent 日志 |
| `get_runtime` | 5 | **承重** | 熔断生效 |
| `CircuitBreaker` / `AgentRuntime` | 0 直接（经 `get_runtime` 间接） | **承重** | 同上；纯逻辑，最厚的单测在这 |
| **`span`** | **0** | **已删除** | 写了没接 |
| **`configure_logging`** | **0** | **已删除** | 写了没接 |

**结论：除了那 50 行（`trace.py` 159 → 112 行），其余每一块都有实际承重。**

### 可选的进一步简化（未做，供决策）

| 可简化项 | 省多少 | 代价 |
|---|---|---|
| 删 `wms://stock/{id}` resource | ~15 行 | 少一个"MCP 第二类能力"演示点 |
| 删 `upsert_stock` 工具 | ~25 行 | 造数据不方便；简历无影响 |
| **删 `orchestrator/graph.py`**（LangGraph 版） | ~180 行 + 更简单的共享逻辑 | **丢掉简历上的 LangGraph 关键词**；但它是实例分裂 bug 的根源 |
| 用现成库替代自建熔断器 | ~200 行 | **丢掉"自建 harness"叙事**；且本项目两个编排器共享 Agent，实例级熔断不适用 |

前两条建议做（顺手），后两条**不建议** —— 它们砍掉的是简历价值本身。

---

## 六、已知问题（未修）

| 问题 | 影响 |
|---|---|
| ~~返回商品数少于 `num_items`~~ | ✅ **已修复**（阶段 A） |
| ~~`models/schemas.py` 的 `agent_results` 子类字段被截断~~ | ✅ **已修复**（2026-09-21）：改用 `SerializeAsAny[AgentResult]`，端到端实测四个 Agent 的专属字段全部出现 |
| ~~`services/feature_store.py` 是一份死代码（没接线）~~ | ✅ **已接线**（2026-09-22）：不是恢复原样，而是**重写**（逐条修掉约 10 个从没跑过所以从没验证过的问题）后接进画像 Agent。详见文末「本轮」 |
| ~~A/B 的 `config` 仍无人消费~~ | ✅ **已删除**（2026-09-21）：A/B 引擎没有真实流量可测，是没接线的冗余实现；前后对比改用 `python/eval/` 的评测集 |
| ~~README 未实测数字~~ | ✅ **已清理**（阶段 G），README 顶部新增「哪些数字能信」 |
| `agents/base_agent.py` 的 `_call_count`/`_error_count` | 保留但已不是健康状态来源，容易误导读者 |
| `mcp[cli]` 未装 | A1（`mcp dev`）无法验证 |

---

### 6.1 返回商品数少于 `num_items`（2026-09-20 实测发现 → **已修复，见阶段 A**）

> ✅ **已修复**。修复方案：Phase 2 复用 Phase 1 的候选集（`candidates=raw_products`）。
> 端到端实测从"要 5 个给 2-3 个"恢复为稳定 5 个。
> 详细原理与代码见 [stages/01-fix-candidate-set.md](stages/01-fix-candidate-set.md)。
>
> 以下保留原始记录，作为"这个 bug 长什么样"的档案。



**症状**：请求 `num_items=5`，稳定只返回 2–3 个商品，**没有任何报错或告警**。

**实测证据**（3 次请求）：

```
inventory:    total_checked=10   available=10     ← 只检查了 P001–P010
product_rec:  candidate_count=15 reranked=5       ← 但重排是从全部 15 个里挑的
最终商品:      ['P007', 'P010'] / ['P007', 'P010'] / ['P007', 'P003', 'P010']
```

**根因**（两处召回集合不一致）：

1. `supervisor.py:74-79` —— Phase 1 的召回是 `product_rec_agent.run(user_profile=None, num_items=num_items*2)`，
   profile 为 `None` 时 `_recall()` 不做排序，直接取 `MOCK_PRODUCTS[:10]` → **P001–P010**
2. `supervisor.py:89` —— 库存 Agent 检查的就是这 10 个
3. `supervisor.py:85-88` —— 但 Phase 2 的重排会**再去召回一次**，这次带着 profile，
   排序后候选集是**全部 15 个**，重排从中挑 5 个（可能包含 P011–P015）
4. `supervisor.py:98` —— `final_products = [p for p in ranked_products if p.product_id in available_ids]`
   → 重排挑中的 P011–P015 **不在库存检查过的集合里，被刷掉**
5. `supervisor.py:99-101` —— `if not final_products` 的兜底**不触发**（因为还剩 2–3 个），
   于是**静默地**少于 `num_items`

**性质**：**项目从一开始就有这个 bug**，不是本轮改动引入的。
第一轮代码勘察（写任何代码之前）就已经记录过这一点：
> "P1 的 `raw_products` 只用于库存调用，真正返回的商品来自 P2 的独立召回。"

本轮的「关闭推理」改动只是让它**更容易暴露** —— 基线时期是 4 个（要 5 个），早就在丢，只是丢得少。

**修复方向**（未做，需要决策）：
- 方案 A：库存检查的对象改成**全部候选**，而不是 P1 那 10 个
- 方案 B：Phase 2 的重排复用 Phase 1 的 `raw_products` 作为候选池（通过 `**kwargs` 传入，不改签名）
- 方案 C：兜底逻辑改成「不足 `num_items` 时补位」

⚠️ 方案 C 单独做有副作用：会把实际缺货的商品补回推荐里（MCP 开启时 P007 会重新出现）。

---

## 七、下一步

**必须先决定的一件事**：要不要修 6.1 那个 bug。

它是**正确性**问题（返回值不符合契约），不是锦上添花。三个修复方向见 6.1，各有副作用，
需要人来定。**在没有决定之前，不建议往上叠新功能** —— 否则后面所有基于"返回 N 个商品"的
测试和文档都会建立在一个不成立的前提上。

决定之后，按优先级：

| # | 事项 | 性质 | 备注 |
|---|---|---|---|
| 0 | **修 6.1** | 正确性 | 需先选方案 |
| 1 | **D1 `recommend_server`** | 补齐「MCP 双侧」 | 半天，复用 `harness/deps.py` |
| 2 | **D9 `docs/mcp-integration.md`** | 文档 | 与 D1 一起收尾 |
| 3 | **诚实化**（M8） | 清理 | README 里未实测的数字是面试风险 |
| 4 | 评测集 + `--baseline`（M1.5 / M7） | 证据 | 每次改动能出前后对比 |
| 5 | token/成本账本（M3 剩余） | 可观测 | |
| 6 | 工具层 + 运营 Copilot（M4/M6） | 新增能力 | **1–2.5 天，最大的一块，可做可不做** |

**关于第 6 项**：它是最稀缺的（自己实现 tool-calling loop），但也是唯一还没开始的。
不做它，「MCP 双侧」叙事仍然成立（Server 侧靠 D1 补齐）；做了它才升级成「三端」。

---

## 附：本轮（2026-09-20 第二阶段）做了什么

1. **`docs/harness-mcp-walkthrough.md`** —— 讲清楚 harness 与 MCP 具体是哪几行代码。
   含三部分：harness 的五步讲法 / 用 P007 走三个 MCP 场景 / 逐条「删掉会怎样」。
2. **`CLAUDE.md`** —— 给 AI agent 的项目说明（此前仓库里没有任何这类文件）。
   重点是把六个踩过的坑写下来，避免下一个 agent 重新踩。
3. **删掉 50 行投机代码** —— `harness/trace.py` 的 `span()` 与 `configure_logging()`，
   生产代码 0 引用。`trace.py` 159 → 112 行，测试 78 → 75。
4. **发现 6.1 那个 bug** —— 在验证"删代码有没有弄坏东西"时实测发现的。

---

## 附：本轮（2026-09-22）做了什么 —— Redis 实时特征层接线

**起因**：README 的「❌ 不可信的」表里长期挂着一行"Redis Sorted Set 实时特征 —— 从没实现"。
上一轮的处理是把它**删掉**（死代码清理），这一轮改成**做出来**。

**做了什么**：

1. **`services/feature_store.py` 重写**（不是恢复原样）。原版从没跑过、也从没被验证过，
   攒了约 10 个问题，逐条修掉：产出的 key 集合和消费端对不上、`record_behavior` 里调了两次
   `time.time()`（payload 的 ts 和 zset 的 score 是两个瞬间）、金额取自 payload 里根本不存在的字段、
   用 `len(窗口内全部成员)` 代替 `ZCOUNT`、把 TTL 当窗口用导致 ZSET 无界增长、
   完全没有 try/except（Redis 一抖动就 500）…… 全文见该文件的模块 docstring。
2. **新增 `python/scripts/seed_behavior.py`** —— 灌确定性行为种子，照 `mcp_servers/init_wms_db.py`
   的模式：显式跑一次、种子确定、**跑完用"消费端会看到的那份数据"读回来自证**。
3. **`user_profile_agent._collect_behavior()` 改成三源合并**（context > Redis > 内置兜底值），
   并返回**三值** `source`（`redis` / `redis_empty` / `fallback`）。
4. **`harness/deps.py` 新增 `get_feature_store()`** —— 和 MCP 客户端同一个口径：
   **开关关闭时返回 None**，连 redis 客户端都不构造（关闭时零新增失败面）。
5. **新增 4 个配置项**（`config/settings.py` + `python/.env.example`），
   开关 `feature_store_enabled` **默认 false**。
6. **评测新增 2 条用例** rec_011（Redis 路径真被走到）/ rec_012（全新用户不被喂假数据），
   用 `requires_feature_store` 标记 —— 开关关闭时自动跳过，默认配置下的通过率不受影响。

**两个坑（值得记住，都写进了 `settings.py` / `.env.example` 的注释）**：

- 本机 Redis 是 **5.0**，**不支持 `HELLO`**，而 redis-py 5+ 默认走 RESP3、建连时先发 HELLO
  → 客户端必须显式 `protocol=2`（RESP2 在 7.x 上同样合法，不是本机专用的 hack）。
- `localhost` 会先解析到 IPv6 的 `::1`，而本机 Redis **只监听 IPv4** → 首次连接挂约 2 秒才回落。
  实测首次 PING：`localhost` **2052.3 ms** vs `127.0.0.1` **2.4 ms**。
  这 2 秒大于请求路径的 0.5s 超时，会把连接掐断 → **每次请求都重连、每次都超时 → 永久降级**，
  看起来像"功能没接上"。所以 URL 必须写 `127.0.0.1`，且启动时 `warmup()` 预热。

**结果**：测试 139 → **168 passed, 1 skipped**；评测 **12/12**；
README 的「❌ 不可信的」表**少了一行**（对照表变短 = 项目变干净）。

---

## 附：本轮（2026-09-22 下午）做了什么 —— 清 P0 / P1 工程缺口

**起因**：对后端做了一次开发完成度审计（核心业务逻辑 / API 接口 /
数据库交互 / 异常处理 / 单元测试五个维度），查出 3 个 P0 + 4 个 P1。
排序依据是"**会不会在演示现场或面试追问时出丑**"，不是工作量。

### P0（都会在现场翻车）

| # | 缺口 | 根因 | 处理 |
|---|---|---|---|
| P0-1 | `docker compose up` 必然失败 | 目录已从 `python/` 改名 `backend/`，但 `build.context` 仍写 `./python` | 改 `./backend`；`start-python.bat` 里的 `cd /d "%~dp0python"` 同因失效，一并修 |
| P0-2 | 容器内以 reload 模式运行 | `Dockerfile` CMD 是 `python main.py`，而 `main.py` 的 `__main__` 硬编码 `reload=True` | CMD 改直接起 uvicorn；`__main__` 的 reload 改由 `ECOM_DEV_RELOAD` 控制，**默认 false** |
| P0-3 | 5 条路由**零**接口级测试 | 168 个单测全在 harness/agent/service 内部，一个 HTTP 请求都不发 | 新增 `tests/test_api.py`（39 条） |

### P1

| # | 缺口 | 处理 |
|---|---|---|
| P1-1 | **`structlog` 从未 `configure()`** —— 文档写的"结构化 JSON 日志"实际是默认彩色文本（原 `configure_logging()` 在"删死代码"那轮因 0 引用被删，删得对，但能力也一起没了） | 新增 `logging_setup.py`，`main.py` 启动时调一次。实测输出确为逐行 JSON，且 request_id 已贯穿每个 Agent 的日志 |
| P1-2 | 5 个零引用依赖 + compose 里两个没人用的容器 | 删 `pymilvus` / `sqlalchemy` / `numpy` / `prometheus-client` / `langchain-community`，删 milvus 与 mysql 服务，删 `milvus_*` / `database_url` 配置项。`httpx` 与 `python-dotenv` **保留**并在清单里写明功能性理由（TestClient / .env 加载） |
| P1-3 | 无鉴权；CORS 为 `allow_origins=["*"]`，而 `/metrics` 会暴露成本与单价指纹 | 新增 `ApiKeyMiddleware`（**默认关闭**，`/health` 与文档路径免鉴权，时序安全比较）；CORS 来源改为配置项，默认是开发期端口 |
| P1-4 | 无全局异常处理器；`AgentResult.error` 里的 `str(exc)` 会外泄内部细节 | 新增 `web/errors.py`：类型化错误 + 4 个处理器，统一 `{code, message, request_id}`；对外一句通用文案，对内完整堆栈 |

### 顺带做掉的一件 P2（因为它是 P1-4 的另一半）

`X-Request-ID` 响应头 + 请求 ID 贯穿：新增 `RequestIdMiddleware`，
并让 `supervisor` / `graph` / `copilot` 复用已绑定的 id
（原先各处无条件 `new_request_id()`，同一次请求在日志里有两个 id）。
现在 **响应头 = 响应体 request_id = 日志 request_id**，实测端到端一致。

### 过程中发现并修掉的三个**新**问题

这三个都是本次改动/验证过程中实测暴露的，此前没人知道：

1. **`.env` 残留旧配置键会让服务起不来。** 删掉 `Settings` 里的
   `milvus_host` / `database_url` 之后，`extra="forbid"` 让启动直接
   `ValidationError`。已清理 `.env`，并在 `.env.example` 顶部写了升级提示。
   **保留严格模式**（不改成 `extra="ignore"`）：静默忽略拼错的变量名
   才是更难查的坑。
2. **compose 里 `environment` 的优先级高于 `env_file`。**
   `ECOM_LLM_API_KEY=${ECOM_LLM_API_KEY:-}` 展开成空串后**覆盖掉了
   `.env` 里的真实 key**，且不报错。靠 `docker compose config`
   看到 `ECOM_LLM_API_KEY: ""` 才发现。现在 `environment` 里只留
   `ECOM_REDIS_URL` 这一项（容器内必须用服务名）。
3. **兜底的 500 处理器读不到 request_id。** `ServerErrorMiddleware`
   永远在最外层，异常穿过 `RequestIdMiddleware` 时 contextvar 已复位，
   `current_request_id()` 返回 `None`。改为**先读 `scope["state"]`、
   再回落 contextvar**；500 还得自己补 `X-Request-ID` 响应头
   （其它三个处理器在中间件内层，头是自动加的）。
   → 被 `test_api.py` 拦下，详见 CLAUDE.md 陷阱 10。

### 未做（明确留给下一轮）

- **P2 剩余项**：`/recommend/graph` 与主接口的契约不一致（无 `response_model`、
  未初始化时用 body 里的 error 字段而不是 HTTP 错误码）；无 `/ready`
  （探活与就绪未分离）；无 CI；无覆盖率统计；无并发测试。
- **文档路径残留**：`CLAUDE.md` / `README.md` / `docker-compose.yml` /
  `start-python.bat` / `.env.example` 已修。但 `docs/` 下更早的阶段文档
  （`mcp-integration-prd.md`、`code-walkthrough.md`、`stages/*.md`、
  `architecture.md`、`interview-guide.md` 等）里仍有约 **72 处** `python/`。
  这些多为历史记录，按 append-only 原则未改写，见 `CLAUDE.md` 第二节的提示。

### 结果

- 测试：**168 passed + 1 skipped → 207 passed + 1 skipped**（新增 39 条接口级测试）
- 接口级覆盖：**0 → 5/5 路由**
- 未实测数字：**0**（原先文档里的"结构化 JSON 日志"这一条已成为事实，而非宣称）
- 实测端到端（真 LLM，重启后）：`/recommend` **200 / 3474.1 ms / 3 商品 / 3 文案**，
  `body request_id == X-Request-ID 头`；404 / 422 / 405 均为统一错误信封

---

## 附：本轮（2026-09-22 傍晚）做了什么 —— 清 P2 + 建 CI

**起因**：上一轮清完 P0/P1 后，报告里还剩 4 条 P2。另外审计的 04.2 节
还列着一条「无限流」没归入 P2，成本低，一并做了。

### P2-A `/recommend/graph` 契约统一

原先这个端点返回一个**缩小版对象**：没有 `response_model`（OpenAPI 里
没有 schema，前端生成不了类型）、没有 `agent_results`、没有 `harness`，
而且出错时返回 `200 + {"error": "Graph not initialized"}` ——
客户端按 status code 判断成就会认为它成功了。

改动：
- 加 `response_model=RecommendationResponse`，**与主接口同一个契约**；
- 新增 `orchestrator/reporting.py`，把 `build_harness_report()` 从
  `SupervisorOrchestrator` 的私有静态方法里提出来共用（graph 依赖另一个
  编排器的私有方法，是错的方向依赖）。顺带发现原方法的 `request_id`
  参数从来没被用到；
- 新增 `graph.build_response()` 做键名归一化：图内部把两次 product_rec
  调用记成 `product_recall` / `rerank`，响应契约里只有 `product_rec`
  （**重排后**的结果，与 supervisor 口径一致）。不归一化的话前端会看到
  一个它不认识的 `product_recall`，而少的那个 `product_rec` 恰好是它要的；
- 未装配时改抛 `ServiceUnavailableError` → **503 + 统一错误信封**；
- 补上与主接口一致的 `_collect_metrics()` —— 否则 `/api/v1/metrics`
  会随"调用者挑了哪个端点"变化，那它就不是系统指标了。

**实测**：两个端点的响应顶层键**完全相同**；graph 的 `agent_results`
归一化为 `['inventory','marketing_copy','product_rec','user_profile']`；
`harness.usage` 有真实数字（3 次 LLM 调用 / 963 in / 207 out / $0.000462）
—— 这同时证明了 `usage_scope()` 能穿过 LangGraph 的节点任务（那是个
真实的风险点：账本靠 contextvar，而节点跑在各自的任务里）。

### P2-B `/ready` 就绪探针（与 `/health` 语义分离）

| | `/health` | `/ready` |
|---|---|---|
| 含义 | **存活**：进程还在、能响应 HTTP | **就绪**：此刻能否履行契约 |
| 检查 | 不检查任何依赖 | 检查流水线是否装配完 |
| 失败处置 | **重启** | **等待**（重启解决不了） |

**关键设计判断**：可选依赖（Redis / MCP）**不参与**就绪判断。
因为本系统整个卖点就是"外部依赖挂了也能降级交付"（Redis→fallback、
MCP→回落 `Product.stock`、LLM→降级结果，全部实测 200）。把它们算进就绪，
会让编排系统摘掉一个其实还在正常降级服务的实例 —— 等于亲手丢掉降级能力。
它们只被**汇报**在 `optional` 里（Redis 那条是真实探测，0.5s 预算）。

另新增 `FeatureStore.ping()`：与 `warmup()` 的区别是预算（请求路径 0.5s
vs 启动 5s）。`/ready` 会被每几秒打一次，用 5s 预算会让探针本身变成负载。
两者共用 `_ping()` 实现，事件名保持不变。

### P2-C CI + 覆盖率统计

`.github/workflows/ci.yml` 两个 job：
`backend`（安装依赖 → `compileall` 语法检查 → `pytest --cov` 门禁）、
`compose-config`（`docker compose config`，专门守 P0-1 那类路径回归）。

**⚠️ 建 CI 之前先做了一次"全新检出"验证，结果发现测试根本跑不起来：**

```
$ pytest tests/          # 全新 clone，没有 .env
ERROR tests/test_api.py - openai.OpenAIError: Missing credentials
```

根因：`main.py` 在**模块级**调 `get_supervisor()`，连锁构造 4 个 Agent
及其 LLM 客户端，而 openai SDK 在**构造期**校验凭据、缺了直接抛。
于是"没配 key"的真实表现是**整个进程起不来、连测试都收集不了**。

而这个项目自己写在 `harness/deps.py` 的设计意图恰恰相反：
> 用 `@lru_cache` 而不是模块级全局变量……模块级全局在 import 时就会构造
> 全部 Agent（包括读 .env、建 LLM 客户端）。而 MCP Server 进程、
> 测试进程未必需要全部依赖。

**模块级那次调用把惰性设计抵消掉了。** 三处修复：
1. `harness/llm.py` 加 `_resolve_api_key()`：缺凭据时返回占位符并报一次
   error，而不是让 SDK 抛。行为变成与"key 过期/被吊销"一致 ——
   客户端能构造、调用时 401、Agent 走 fallback，也就是**本来就设计好的
   降级路径**。少一个凭据不等于服务不能启动，它等于所有 Agent 降级。
2. `main.py` 去掉模块级的 `get_supervisor()`，改在路由里取
   （`@lru_cache` 之后就是一次字典查找）—— 让"import main"不再有副作用。
3. `lifespan` 里补一条 `app.llm_key_missing` 的 error 日志（带处置建议），
   保证误配在运行时一眼可见。

**验证**：模拟 git clone（排除 `.venv` / `.env` / `*.db`）后
`compileall` 通过、**231 passed + 1 skipped**、覆盖率 **85.61% ≥ 83**。
同时给这条加了守卫 —— CI 里显式设 `ECOM_LLM_API_KEY: ""`，
谁再把凭据变成测试的前提，CI 会红。

覆盖率配置在 `backend/pyproject.toml`：
- 排除 `tests/`、`eval/`、`scripts/` —— 后两者是 **CLI 入口**，正确的
  验证方式是"真的跑一遍并产出 `eval_results/`"，不是单测。
- **实测两个数字都记下来**：不排除时 **75.56%**，排除后 **85.42%**。
  排除掉的是"不适合用单测衡量"的部分，不是"测不过去的"部分。
- `fail_under = 83`（比实测留约 2.4 个点）。贴着实测设会让任何正常改动
  都变红，而一个天天变红的门禁等同于没有门禁。

### P2-D 并发正确性测试

新增 `tests/test_concurrency.py`（8 条）。价值不在于"多测了几个函数"，
而在于它守卫 `harness/breaker.py` 里一条**可被证伪的设计声明**：

> 单线程事件循环下 `allow()` 与 `record()` 之间没有 await 点，所以不需要锁。

一旦有人在两者之间插入 await（给 record 加个上报、给 allow 加个异步
配置读取），就会出现丢失更新、以及 half_open 下放过多个探测。
测试用 `await asyncio.sleep(0)` 在桩 Agent 里制造真交错，断言：
- 20 个并发失败只产生**一次** `agent.circuit_tripped`（状态迁移幂等）
- 20 个并发失败**一个都不丢**（窗口大小 == 执行次数）
- 成功/失败混合并发时三个派生量彼此自洽
- 被熔断拒绝的调用**不撑大窗口**（否则熔断器再也无法闭合）
- half_open 下 10 个并发只放行**恰好 1 个**探测 ← 最容易被并发打穿的地方
- 按 agent 名隔离：A 挂了不会连坐 B
- 短路路径几乎零耗时（并发下同样成立）

写这条时踩到一个自己的想当然：探测成功后窗口是 **0** 不是 1 ——
`_close()` 会清空窗口（恢复后重新开始统计）。

### 附带：限流（审计 04.2 的缺失项）

`web/ratelimit.py`，进程内**滑动窗口**。为什么不是固定窗口：
固定窗口在边界处有经典漏洞（第 59 秒打满 N 次、第 61 秒再打满 N 次
= 两秒内放过 2N 次，即设计速率的两倍）。

- **默认关闭**（`ECOM_RATE_LIMIT_ENABLED=false`）—— 与其它新能力同口径，
  也因为 `eval/runner.py` 是串行快速连打做评测的，默认开启会把它卡在 429；
- 按**凭据哈希**分桶，没有凭据才退到客户端 IP。**不读 `X-Forwarded-For`**
  （可伪造，等于把"换个假 IP 就绕过"送出去）；
- `/health`、`/ready` 免限流；
- 429 走统一错误信封 + `Retry-After` + `X-RateLimit-*`；
- 桶会**定期清扫**（每 512 次请求）—— "每个陌生 IP 留一个空 deque"
  是这类限流器最容易漏的内存泄漏；
- 中间件顺序调整为 `CORS → RequestId → RateLimit → ApiKey → 路由`：
  限流在鉴权**外层**（未通过鉴权的洪水也被限流），在请求 ID **内层**
  （429 也带 `X-Request-ID`）。

**实测**（阈值 6 / 30s）：第 7 个非免检请求 → **429**，
`code=rate_limited`、`Retry-After: 25`、`X-RateLimit-Limit: 6`、
`X-RateLimit-Remaining: 0`、`X-Request-ID` 与 body 一致；
期间 `/health` 与 `/ready` 仍然全 200。

### 结果

- 测试：**207 → 231 passed + 1 skipped**（新增 8 条并发 + 16 条接口/中间件）
- 路由：**5 → 6**（新增 `/ready`），且两条推荐路径**契约完全一致**
- 覆盖率：**0（未测过）→ 85.42%**，CI 门禁 83
- CI：**从无到有**（两个 job，其中一个专门守 compose 路径回归）
- 新增依赖：`pytest-cov`（唯一一项）


