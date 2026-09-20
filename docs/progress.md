# 二次开发进度总览

> 最后更新：2026-09-20
> 目标：投「AI 应用 / Agent 开发岗（校招）」，突出 **harness 工程能力** 与 **MCP 双侧**
> 原始计划：`docs/mcp-integration-prd.md`（MCP）、`docs/extension-guide.md`（二次开发指南）

**一句话现状**：harness 骨架已立起来并全部有实测证据；MCP **客户端侧**完成并通过 A4/A5，
**服务端侧（D1）尚未开始**。

---

## 一、计划里程碑（M0–M8）

| # | 内容 | 状态 | 关键产出 / 证据 |
|---|---|---|---|
| **M0** | 基线固化 | ✅ | 基线 p50 = **48,015 ms**；`pytest` 进依赖；Java/Go 删除已提交 |
| **M0b** | tool-calling 探针 | ✅ | 模型支持原生 tool calling；**langchain-core 原生认 MCP schema，适配器代码量为 0** |
| **M1** | 请求级追踪 | ✅ | `request_id` 贯穿；`agent.retry` 事件上线（并据此推翻我一个错误结论） |
| **M1.5** | 评测运行器 | ❌ **未做** | 计划里排在 M1 之后，被 M2 的阻塞问题插队 |
| **M2** | 真超时 + 熔断 | ✅ | 故障注入 4/4 HTTP 200；熔断第 3 次请求 **0ms** 短路 |
| **M3** | token / 成本账本 | ⚠️ **部分** | `build_chat_model` 工厂已做（提前）；**`MeteredChatOpenAI` 记账未做** |
| **M4** | 工具层 + 组合根 | ⚠️ **部分** | `harness/deps.py` 组合根已做；**`ToolSpec`/`ToolRegistry` 未做** |
| **M5** | MCP 客户端侧 | ✅ | 见下方 D1–D10 |
| **M6** | 运营 Copilot | ❌ **未做** | 依赖 M4 的工具层 |
| **M7** | 评测扩充 + 延迟取证 | ⚠️ **部分** | 延迟优化已拿到 10.4 倍实测；**评测集未建** |
| **M8** | 诚实化（README/docs） | ❌ **未做** | 已知 README 仍有 CTR +15%、P99<2s 等未实测数字 |

图例：✅ 完成并验证 · ⚠️ 部分完成 · ❌ 未开始

---

## 二、MCP PRD 交付物与验收项

### 交付物（D1–D10）

| # | 交付物 | 状态 | 说明 |
|---|---|---|---|
| D1 | `mcp_servers/recommend_server.py` | ❌ **未做** | 把项目能力**对外暴露**给外部 MCP Host —— 「双侧」的服务端那一半 |
| D2 | `mcp_servers/wms_server.py` | ✅ | 4 工具 + 1 resource，stdio |
| D3 | `mcp_servers/init_wms_db.py` | ✅ | 15 商品，**9 个与目录库存故意不同**（A5 的根据） |
| D4 | `services/mcp_client.py` | ✅ | 每次调用短连接，永不抛异常给调用方 |
| D5 | `agents/inventory_agent.py` 接线 | ✅ | 唯一接线点 |
| D6 | 配置开关 | ✅ | 4 个 `ECOM_MCP_*`，全部默认 `false` |
| D7 | `main.py` 生命周期管理 | ⚠️ **按设计不需要** | 选了短连接 ⇒ 进程内无常驻 MCP 连接可管。**这是我的判断，与原 PRD 有出入** |
| D8 | 测试 | ✅ | `tests/test_mcp_wms.py` + `tests/test_inventory_mcp.py` |
| D9 | `docs/mcp-integration.md` 使用文档 | ❌ **未做** | |
| D10 | 依赖 | ✅ | `mcp>=2.0,<3`（v2 稳定线，不装 `langchain-mcp-adapters`） |

### 验收项（A1–A7）

| # | 验收项 | 状态 | 证据 |
|---|---|---|---|
| A1 | `mcp dev` 可发现 Server | ⚠️ **未验证** | `mcp` 命令行存在，但缺 `typer`（需 `pip install mcp[cli]`） |
| A2 | 工具逻辑（进程内 Client） | ✅ | 12 条测试，不起子进程不占端口 |
| A3 | 端到端 200 + 日志可见 MCP 调用 | ✅ | `inventory.stock_source source=mcp` |
| A4 | **降级：MCP 挂掉仍 200** | ✅ | `source="fallback"`，商品文案照常返回 |
| A5 | **契约：证明真的走了 MCP** | ✅ | 见下方「A5 证据」 |
| A6 | 回归探针 | ⚠️ **未做** | PRD 说探针归档在 `D:\devlop\_archive\probes_20260920`，该目录里**没有** `probe_recommend.py` |
| A7 | 开关关闭时零影响 | ✅ | 关时不构造客户端，行为与集成前一致 |

---

## 三、关键实测数字

所有数字均为实测，可复现（脚本在 `d:\tmp\`，结果在 `eval_results/`）。

### 延迟（5 次请求 p50）

| Agent | 改动前 | 改动后 | 改善 |
|---|---|---|---|
| **全链路** | **48,015 ms** | **4,597 ms** | **10.4×** |
| `user_profile` | 8,819 ms | 961 ms | 9.2× |
| `product_rec` | 29,700 ms | 728 ms | **40.8×** |
| `marketing_copy` | 5,059 ms | 2,900 ms | 1.7×（**故意保留推理**） |
| 波动范围 | 39.4 – 87.5 s | 4.0 – 13.8 s | |

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

### A5 证据（去掉 LLM 变量，直接对比 `InventoryAgent`）

```
MCP 开  13/15 可用   source=mcp
MCP 关  15/15 可用   source=fallback
唯一差异：P007、P014 —— 恰好是 WMS 库存为 0 的两个（目录分别说 2000 / 100）
```

> 种子数据**故意**与 `Product.stock` 不一致就是为了这个：若两者相同，
> MCP 通与不通输出完全一致，测试全绿也说明不了任何事。

### 工程指标

- 测试：**5 → 78**
- commit：6 个（每个都是独立可验证的单元）
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

## 五、已知问题（未修）

| 问题 | 影响 |
|---|---|
| `models/schemas.py:92` 声明 `dict[str, AgentResult]` | 子类字段（`profile`/`products`/`copies`/`low_stock_alerts`）**被静默截断**，HTTP 响应里看不到。`data.source` 能出来是因为 `data` 是基类字段 |
| `services/feature_store.py`（117 行）从未被实例化 | README 宣称的「Redis 实时特征」是假的 |
| A/B 的 `config` 仍无人消费 | 实验**不影响任何行为**（`assign_thompson` 也从未被调用） |
| README 未实测数字 | CTR +15% / 文案点击率 +23% / P99<2s / 三语言 |
| `agents/base_agent.py` 的 `_call_count`/`_error_count` | 保留但已不是健康状态来源，容易误导读者 |
| `mcp[cli]` 未装 | A1（`mcp dev`）无法验证 |

---

## 六、下一步

按优先级：

1. **D1 `recommend_server`** —— 完成「MCP 双侧」这句话的服务端一半（用户明确要的）
2. **D9 `docs/mcp-integration.md`** —— 与 D1 一起收尾
3. **工具层 `ToolSpec`/`ToolRegistry`**（M4 剩余）—— Copilot 的前置
4. **运营 Copilot**（M6）—— MCP Host 端的多轮 tool-calling loop，最难也最稀缺
5. **token/成本账本**（M3 剩余）
6. **评测集 + `--baseline` 对比**（M1.5 / M7）
7. **诚实化**（M8）
