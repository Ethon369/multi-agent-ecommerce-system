# 🛒 多Agent电商推荐与营销系统

> **面向小白的企业级 AI Agent 项目** — 从零理解 Multi-Agent 架构，配套八股文 + 简历模板 + STAR面试话术，找工作全流程覆盖。

[![Python](https://img.shields.io/badge/Python-3.14-blue?logo=python)](python/)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)

---

## ⚠️ 先读这一段：本文档里的数字哪些能信

这份 README 是分两个阶段写的。**早期写下的那批数字多数没有实测支撑**，
后来做二次开发时逐个核对过。为了不让你在面试里被问穿，这里先说清楚。

### ✅ 可信的（有实测、可复现）

出处：`eval_results/` 下的评测报告，用 `python -m eval.runner` 可重新生成。

| 指标 | 实测值 |
|---|---|
| 全链路延迟 p50 | **2,522 ~ 2,788 ms**（优化前 48,015 ms，约 17 倍） |
| 延迟 p95 | **3,796 ~ 3,997 ms** |
| 单次推荐成本 | **$0.00056 ~ $0.00061** |
| 评测通过率 | **10/10** |
| 单元测试 | **144 个** |
| 故障注入 | 注入超时后 **4/4 请求仍是 HTTP 200** |
| 熔断短路 | 打开后 **0 ms** 返回 |

> 数字写成区间是因为**同一配置多次运行本来就有波动**（LLM 输出长度不确定）。
> 这是实测值，不是目标值 —— 用 `python -m eval.runner` 可以自己复现。

### ❌ 不可信的（早期写的目标值或设想，与代码不符）

| 说法 | 实际情况 |
|---|---|
| "CTR 提升 15%、文案点击率提升 23%" | **没有任何数据支撑**。A/B 引擎的 `config` 甚至从未被读取 —— 实验目前不影响任何行为 |
| "P99 < 2s" | 从未达到。实测 p95 是 3,997 ms |
| "三语言实现（Python/Java/Go）" | **Java/Go 实现已删除**，只保留 Python |
| "Redis Sorted Set 实时特征" | **从未实现**。曾有一份 117 行的 `services/feature_store.py`，但从未被实例化，已在死代码清理中删除 |
| "Milvus 向量检索" | `pymilvus` 在依赖里，但**代码里从未 import** |
| "MySQL 实时库存" | 实际是 **SQLite + MCP**（这是二次开发时才真正接上的） |
| "重试最多 3 次" | 实际是 **2 次尝试**（即只重试 1 次）—— 参数原名 `max_retries` 却表示总尝试次数，已改名为 `max_attempts` |
| "熔断器：错误率 > 50% 自动熔断，返回缓存结果" | 二次开发**才真正实现**，且策略是"滑动窗口内失败计数 ≥ 5"，不是错误率 |

> **整段二次开发的经过、每一步的实测数字和设计取舍**，见
> [docs/progress.md](docs/progress.md)（进度总览）与 [docs/stages/](docs/stages/)（分阶段技术文档）。
>
> **harness 与 MCP 具体是哪几行代码**，见 [docs/harness-mcp-walkthrough.md](docs/harness-mcp-walkthrough.md)。

### 🔧 经过二次开发后新增的能力

| 能力 | 说明 |
|---|---|
| **MCP 三端** | Server（自建 2 个）+ Client（消费 WMS）+ Host（运营 Copilot 多轮工具调用） |
| **Agent 运行时保障** | 真超时、滑动窗口熔断、请求级 trace 贯穿、两级降级 |
| **可观测性** | 每个 Agent 的 token 与成本分摊、熔断状态、工具清单 |
| **评测闭环** | 10 条 golden set + 确定性门禁 + 非门禁 LLM 裁判 + 前后对比 |

---

## 📖 目录

1. [这个项目是什么？](#-这个项目是什么)
2. [系统架构（看图秒懂）](#-系统架构看图秒懂)
3. [四大核心 Agent 详解](#-四大核心-agent-详解)
4. [关键代码展示](#-关键代码展示)
5. [快速上手运行](#-快速上手运行)
6. [API 接口文档](#-api-接口文档)
7. [项目文件结构](#-项目文件结构)
8. [面试资料索引](#-面试资料索引)
9. [面试八股文精选](#-面试八股文精选10题)
10. [简历写法（直接复制）](#-简历写法直接复制)
11. [参考资料与致谢](#-参考资料与致谢)

---

## 🤔 这个项目是什么？

### 用一句话解释

> 用 AI Agent 技术，让电商平台的**推荐 + 文案 + 库存**三个系统协同工作，像一个聪明的"AI 运营团队"一起为每位用户生成个性化推荐结果。

### 它解决了什么问题？

传统电商推荐系统存在三大痛点：

| 痛点 | 传统做法 | 本项目做法 |
|------|---------|---------|
| 推荐结果和库存脱节 | 推荐了缺货商品 | **库存 Agent** 实时校验，缺货自动剔除 |
| 营销文案千篇一律 | 所有人看同一段广告语 | **文案 Agent** 根据用户画像生成个性化文案 |
| 各系统各自为战 | 推荐、文案、库存三套系统互不感知 | **Supervisor** 统一编排，结果实时互相影响 |

### 技术关键词（面试常考）

`Multi-Agent` · `Supervisor模式` · `LangGraph` · `asyncio并行` · `MCP（Server/Client/Host）` · `A/B Testing` · `Thompson Sampling` · `熔断与降级` · `token 成本核算` · `DeepSeek LLM`

---

## 🏗 系统架构（看图秒懂）

### 整体架构图

```
┌─────────────────────────────────────────────────────────────────┐
│                         用户发起推荐请求                           │
│                    {"user_id": "u001", "num_items": 5}           │
└───────────────────────────────┬─────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Supervisor 协调Agent                           │
│                  (python/orchestrator/supervisor.py)              │
│                                                                   │
│  ════════════════ Phase 1: 并行执行 ═══════════════════           │
│  ┌──────────────────────┐    ┌──────────────────────┐            │
│  │   用户画像 Agent      │    │   商品召回 Agent      │            │
│  │  user_profile_agent  │    │  product_rec_agent   │            │
│  │  ──────────────────  │    │  ────────────────── │            │
│  │  行为数据(内置兜底)   │    │  多路召回(规则排序)     │            │
│  │  RFM模型 → 用户分群   │    │  返回候选商品列表     │            │
│  └──────────┬───────────┘    └──────────┬──────────┘            │
│             │                           │                         │
│  ════════════════ Phase 2: 并行执行 ═══════════════════           │
│  ┌──────────────────────┐    ┌──────────────────────┐            │
│  │   LLM重排 Agent      │    │   库存决策 Agent      │            │
│  │  (product_rec再次调用)│    │   inventory_agent    │            │
│  │  ──────────────────  │    │  ────────────────── │            │
│  │  用户画像 × 商品属性  │    │  MCP → 实时库存查询   │            │
│  │  LLM精排，返回TopN   │    │  过滤缺货，输出限购策略│            │
│  └──────────┬───────────┘    └──────────┬──────────┘            │
│             │                           │                         │
│  ════════════════ Phase 3: 串行执行 ═══════════════════           │
│             └──────────────┬────────────┘                         │
│                            ▼                                      │
│             ┌──────────────────────────────┐                      │
│             │      结果聚合器               │                      │
│             │  库存过滤 → 排序合并 → TopN   │                      │
│             └──────────────┬───────────────┘                      │
│                            ▼                                      │
│             ┌──────────────────────────────┐                      │
│             │   营销文案 Agent              │                      │
│             │  marketing_copy_agent        │                      │
│             │  ────────────────────────── │                      │
│             │  5套Prompt模板 × 用户分群    │                      │
│             │  LLM生成 + 广告法合规校验    │                      │
│             └──────────────┬───────────────┘                      │
│                            ▼                                      │
│             ┌──────────────────────────────┐                      │
│             │   A/B 测试引擎               │                      │
│             │  用户ID哈希分桶              │                      │
│             │  Thompson Sampling 动态调优  │                      │
│             └──────────────┬───────────────┘                      │
└──────────────────────────────┬──────────────────────────────────┘
                               ▼
              ┌─────────────────────────────────┐
              │  个性化推荐响应（返回给用户）      │
              │  商品列表 + 个性化文案 + 实验分组 │
              └─────────────────────────────────┘
```

### 为什么用 Supervisor 模式？

Supervisor 模式是 Multi-Agent 系统中最主流的编排方式之一：

```
Supervisor 模式                     Handoffs 模式
──────────────────────              ──────────────────────
   Supervisor（中枢）                 Agent A → Agent B
    ┌────┬────┬────┐                       ↓
    ▼    ▼    ▼    ▼                 Agent B → Agent C
   A    B    C    D                        ↓
    └────┴────┴────┘                 Agent C → ...
    结果聚合 → 响应

✅ 集中控制，流程清晰          ✅ 去中心化，灵活
✅ 并行执行，延迟低            ✅ 适合对话/开放式任务
✅ 异常统一处理                ❌ 状态管理复杂
本项目采用 Supervisor 模式
```

---

## 🤖 四大核心 Agent 详解

### Agent 1：用户画像 Agent

**文件**：[`python/agents/user_profile_agent.py`](python/agents/user_profile_agent.py)

**它做什么？**

把用户的历史行为数据（点击、购买、收藏）转化成结构化的"用户画像"，供其他 Agent 使用。

**核心逻辑（简化）**：

```python
# Step 1：获取用户行为数据
# ⚠️ 注意：这里用的是【写死的演示数据】，不是真实行为数据。
#    曾经有一份 117 行的 Redis 实现（services/feature_store.py），但从未被
#    实例化过，已删除 —— 与其留一个永远走不到的分支，不如让"数据是假的"
#    这件事在代码里一眼可见。想接真实行为源，只改 _collect_behavior 一处。
behavior = await self._collect_behavior(user_id, context)   # 内置兜底数据
# 返回: {"clicks_1h": 12, "purchases_7d": 3, "categories": ["手机", "耳机"]}

# Step 2：调用 LLM 分析，输出结构化画像
prompt = f"用户行为数据: {behavior}\n请分析用户分群和RFM得分，输出JSON"
profile_json = await llm.invoke(prompt)
# 输出: {"segments": ["active", "price_sensitive"], "rfm_score": {"recency": 0.8}}

# Step 3：返回 UserProfile 对象
return UserProfile(user_id=user_id, segments=["active"], rfm_score=...)
```

**关键技术**：
- **Redis Sorted Set**：`ZADD user:u001:clicks {时间戳} {商品ID}`，支持滑动窗口查询
- **RFM 模型**：Recency（最近购买时间）× Frequency（购买频率）× Monetary（消费金额）
- **用户分群**：新客 / VIP / 价格敏感 / 活跃 / 流失风险，共 5 类

---

### Agent 2：商品推荐 Agent

**文件**：[`python/agents/product_rec_agent.py`](python/agents/product_rec_agent.py)

**它做什么？**

两阶段推荐：先"召回"大量候选商品，再用 LLM 精排出最合适的 TopN。

```
多路召回策略
  ├── 协同过滤（买了A也买了B）
  ├── 向量检索（未实现：pymilvus 在依赖里但代码从未 import）
  ├── 热度策略（最近7天热卖）
  └── 新品策略（上架30天内）
        │
        ▼（去重合并，候选集）
  LLM 精排
  │ Prompt: "用户是价格敏感型，偏好手机配件，以下10件商品请排序..."
  │ 输出: 按相关性从高到低排列的商品 ID 列表
        │
        ▼
  TopN 商品列表（交给库存 Agent 过滤）
```

---

### Agent 3：营销文案 Agent

**文件**：[`python/agents/marketing_copy_agent.py`](python/agents/marketing_copy_agent.py)

**它做什么？**

根据用户画像自动选择合适的文案风格模板，调用 LLM 生成个性化文案，并做广告法合规校验。

```python
# 5套模板 × 用户分群
TEMPLATES = {
    "new_user":        "首单专属福利，{product}立减{discount}元！",
    "vip":             "尊享会员特权，{product}专属价{price}，品质之选。",
    "price_sensitive": "今日限时抢购！{product}历史最低价，仅剩{stock}件！",
    "active":          "根据您的浏览偏好，为您精选 {product}，好评率{rating}%",
    "churn_risk":      "好久不见！{product}为您专属保留，点击领取优惠券",
}

# 广告法合规校验（过滤违禁词）
BANNED_WORDS = ["最好", "第一", "最便宜", "绝对", "100%"]
```

---

### Agent 4：库存决策 Agent

**文件**：[`python/agents/inventory_agent.py`](python/agents/inventory_agent.py)

**它做什么？**

查询商品实时库存，过滤缺货商品，输出限购策略和补货预警。

```python
# 输入: 推荐商品列表 [P001, P002, P003, ...]
# 通过 MCP 查询实时库存（WMS Server，SQLite 支撑）
# 输出:
{
    "available_products": ["P001", "P003"],   # 有货商品
    "inventory_alerts": [                      # 库存预警
        {"product_id": "P001", "stock": 5, "warning": "库存紧张"}
    ],
    "purchase_limits": {                       # 限购策略
        "P001": 2  # 每人最多买2件
    }
}
```

---

## 🌐 关于"多语言实现"

> **本项目的 Java 与 Go 实现已在二次开发时删除。**
>
> 删除理由：它们是同样逻辑的平行实现，既没有 MCP 也没有任何运行时保障层，
> 对 AI 应用岗不能加分，反而会在面试中分散注意力 ——
> 并需要额外解释"为什么 Java/Go 侧没有这些能力"。
>
> 现在只有 Python 一份实现，代码更少、更深，也更好讲。

## 💻 关键代码展示

### Supervisor 并行编排（Python 核心代码）

**文件**：[`python/orchestrator/supervisor.py`](python/orchestrator/supervisor.py)

```python
class SupervisorOrchestrator:
    """Supervisor 编排器 — 并行分发 + 聚合模式"""

    async def recommend(self, request: RecommendationRequest) -> RecommendationResponse:
        start = time.perf_counter()

        # ① A/B 实验分组（在最开始就决定用哪套策略）
        experiment = self.ab_engine.assign(request.user_id)

        # ② Phase 1：用户画像 + 商品召回 并行执行
        profile_result, rec_result = await asyncio.gather(
            self.user_profile_agent.run(user_id=request.user_id, context=request.context),
            self.product_rec_agent.run(user_profile=None, num_items=request.num_items * 2),
        )
        # asyncio.gather() 让两个 IO 密集型任务同时跑，总耗时 ≈ max(两者耗时)

        # ③ Phase 2：LLM重排 + 库存校验 并行执行
        rerank_result, inventory_result = await asyncio.gather(
            self.product_rec_agent.run(user_profile=user_profile, num_items=request.num_items),
            self.inventory_agent.run(products=raw_products),
        )

        # ④ 库存过滤：只保留有货商品
        available_ids = set(getattr(inventory_result, "available_products", []))
        final_products = [p for p in ranked_products if p.product_id in available_ids]

        # ⑤ Phase 3：文案生成（需要前两步结果，所以串行）
        copy_result = await self.marketing_copy_agent.run(
            user_profile=user_profile,
            products=final_products,
        )

        # ⑥ 汇总响应
        total_latency = (time.perf_counter() - start) * 1000
        return RecommendationResponse(
            products=final_products,
            marketing_copies=copies,
            experiment_group=experiment.get("group", "control"),
            total_latency_ms=total_latency,  # 实测 p50 约 2788ms（不是"目标 2s"）
        )
```

> 💡 **小白解读**：`asyncio.gather()` 就像你同时开了两个网页，而不是等一个加载完再开另一个。两个 Agent 并行跑，总延迟约等于最慢那个 Agent 的耗时，而不是两者相加。

---

### A/B 测试引擎（Thompson Sampling）

**文件**：[`python/services/ab_test.py`](python/services/ab_test.py)

```python
class ABTestEngine:
    """
    流量分桶 + Thompson Sampling 多臂赌博机
    
    原理：像赌场里的老虎机，哪台赢的多就多拉哪台。
    算法自动把更多流量分给表现好的实验组。
    """

    def assign(self, user_id: str) -> dict:
        # 用户ID哈希取模 → 保证同一用户每次进同一个实验组（一致性）
        bucket = int(hashlib.md5(user_id.encode()).hexdigest(), 16) % 100
        
        if bucket < 60:
            return {"group": "control", "strategy": "collaborative_filter"}
        elif bucket < 80:
            return {"group": "treatment_llm", "strategy": "llm_rerank"}
        else:
            return {"group": "treatment_vector", "strategy": "vector_search"}

    def record_click(self, user_id: str, clicked: bool):
        # Thompson Sampling: 点击了就更新 Beta 分布参数
        group = self.assignments.get(user_id, "control")
        if clicked:
            self.alpha[group] += 1   # 成功次数 +1
        else:
            self.beta[group] += 1    # 失败次数 +1
        # 下次分配流量时，胜率高的组会自动获得更多流量
```

---

### Agent 基类：重试 + 降级（可靠性保障）

**文件**：[`python/agents/base_agent.py`](python/agents/base_agent.py)

```python
class BaseAgent(ABC):
    """所有 Agent 的基类 — 模板方法模式"""
    
    MAX_RETRIES = 3
    RETRY_DELAY = 1.0  # 秒，指数退避

    async def run(self, **kwargs) -> AgentResult:
        """公开方法：封装了计时、重试、降级"""
        start = time.perf_counter()
        try:
            return await self._retry_execute(**kwargs)
        except Exception as e:
            # 全部重试失败 → 降级（返回默认结果，不影响其他 Agent）
            logger.warning(f"{self.name} fallback triggered: {e}")
            return self._fallback(**kwargs)

    async def _retry_execute(self, **kwargs) -> AgentResult:
        """指数退避重试"""
        for attempt in range(self.MAX_RETRIES):
            try:
                return await asyncio.wait_for(
                    self._execute(**kwargs),
                    timeout=self.timeout,  # 每个 Agent 独立超时控制
                )
            except asyncio.TimeoutError:
                if attempt < self.MAX_RETRIES - 1:
                    await asyncio.sleep(self.RETRY_DELAY * (2 ** attempt))  # 1s, 2s, 4s
        raise RuntimeError(f"{self.name} failed after {self.MAX_RETRIES} retries")

    @abstractmethod
    async def _execute(self, **kwargs) -> AgentResult:
        """子类只需实现这个方法，写业务逻辑即可"""
```

> 💡 **小白解读**：就像打电话打不通会重拨，第1次立刻重拨，第2次等2秒，第3次等4秒（指数退避）。如果全失败了，就返回一个"说得过去的默认结果"（降级），保证整个系统不崩溃。

---

### 运行时保障层（二次开发新增）

**文件**：[`python/agents/base_agent.py`](python/agents/base_agent.py)

> ⚠️ **上面那段 BaseAgent 代码是早期写的示意，参数名与实际不符**
> （实际是 `max_attempts`，且含义为总尝试次数而非重试次数）。
> 二次开发后 `run()` 的真实结构是"熔断门 → 超时 → 重试 → 记账 → 降级"：

```python
async def _run_once(self, **kwargs) -> AgentResult:
    runtime = get_runtime()

    # ① 熔断门：连续失败达阈值就先别打这个下游了
    if not runtime.allow(self.name):
        return self._fallback(latency_ms, CircuitOpenError(self.name))

    try:
        # ② 期限：self.timeout 是【整个 run() 的总预算】，不是单次尝试预算
        result = await asyncio.wait_for(
            self._retry_execute(**kwargs),   # ③ 内含 tenacity 指数退避重试
            timeout=self.timeout,
        )
        runtime.record(self.name, True)      # ④ 记账，喂给熔断窗口
        return result
    except TimeoutError:
        logger.error("agent.timeout", ...)   # ⑤ 超时单独记事件，与"模型报错"区分开
        runtime.record(self.name, False)
        return self._fallback(latency_ms, TimeoutError(...))
```

> 💡 **为什么这个顺序不能改**：熔断必须在重试**外层**。
> 放内层的话，每个 attempt 都会重新求值一次门禁（重试风暴照样穿透），
> 而且统计的是"尝试失败数"而非"调用结果数"，把失败信号放大数倍。
>
> 完整原理见 [docs/harness-mcp-walkthrough.md](docs/harness-mcp-walkthrough.md)。

---

## 🚀 快速上手运行

### 前置条件

- Python 3.11+ / Java 17+ / Go 1.22+（选一个语言即可）
- 申请 LLM API Key（推荐 [MiniMax](https://www.minimax.chat/) 或 [阿里通义](https://dashscope.aliyun.com/)，有免费额度）

---

### Python 版（推荐小白从这里开始）

```bash
# 1. 克隆项目
git clone https://github.com/bcefghj/multi-agent-ecommerce-system.git
cd multi-agent-ecommerce-system/python

# 2. 创建虚拟环境（避免依赖冲突）
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. 安装依赖
pip install -r requirements.txt

# 4. 配置 API Key
cp .env.example .env
# 用记事本/VS Code 打开 .env，填入你的 LLM_API_KEY

# 5. 启动服务
python main.py
# 看到 "Uvicorn running on http://0.0.0.0:8000" 就成功了

# 6. 测试推荐接口
curl -X POST http://localhost:8000/api/v1/recommend \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user_001",
    "scene": "homepage",
    "num_items": 5,
    "context": {
      "recent_views": ["手机", "耳机"],
      "avg_order_amount": 500
    }
  }'
```

---

### Java 版

```bash
cd multi-agent-ecommerce-system/java

# 1. 配置 API Key（编辑 src/main/resources/application.yml）
#    找到 ecommerce.llm.api-key，填入你的 key

# 2. 构建并启动（需要 Maven，可以用 IDEA 直接导入运行）
mvn spring-boot:run

# 3. 测试
curl -X POST http://localhost:8080/api/v1/recommend \
  -H "Content-Type: application/json" \
  -d '{"userId": "user_001", "numItems": 5}'
```

---

### Go 版

```bash
cd multi-agent-ecommerce-system/go

# 1. 设置环境变量
export ECOM_LLM_API_KEY=your_api_key_here
export ECOM_LLM_BASE_URL=https://api.minimax.chat/v1

# 2. 运行
go run cmd/main.go

# 3. 测试
curl -X POST http://localhost:8080/api/v1/recommend \
  -H "Content-Type: application/json" \
  -d '{"user_id": "user_001", "num_items": 5}'
```

---

### Docker 部署

```bash
# 在项目根目录运行
docker-compose up -d
docker-compose ps

# 服务地址
# Python API:  http://localhost:8000
```

> ⚠️ **关于 `docker-compose.yml` 里的 Redis / Milvus / MySQL：**
> 那三个服务目前**代码里都没有连**（`redis` / `pymilvus` / `sqlalchemy`
> 在依赖里但从未 import）。启起来不影响功能，但也不产生任何作用。
>
> 库存数据现在走的是 **SQLite + MCP**，不依赖 MySQL
> （见 [docs/mcp-integration.md](docs/mcp-integration.md)）。

---

## 📡 API 接口文档

### 接口列表

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/v1/recommend` | 核心推荐接口（确定性链路） |
| `POST` | `/api/v1/recommend/graph` | 同一件事的 LangGraph 版 |
| `POST` | `/api/v1/copilot` | ★ **运营助手**：模型自主决定调用哪些工具 |
| `GET` | `/api/v1/experiments` | A/B 实验状态 |
| `GET` | `/api/v1/metrics` | 指标：Agent + 熔断 + token/成本 + 工具清单 |
| `POST` | `/api/v1/experiments/{id}/outcome` | 记录实验结果 |
| `GET` | `/health` | 健康检查 |

**`/api/v1/recommend` 的响应里有 `harness` 段**（顶层），含：

```json
"harness": {
  "usage": { "llm_calls": 3, "input_tokens": 871, "output_tokens": 2327,
             "cost_usd": 0.003054, "cost_known": true,
             "by_agent": { "marketing_copy": {...} } },
  "agents": { "user_profile": {"success": true, "breaker_state": "closed"} },
  "breakers": { "user_profile": {"state": "closed", "failures_in_window": 0} }
}
```

> `cost_usd` 为 `null` 表示**查不到该模型的单价**，不是"免费" ——
> 一个看起来精确但其实是编的成本数字，比没有数字更危险。

**`/api/v1/copilot` 示例**：

```json
POST /api/v1/copilot
{"message": "哪些商品快断货了？"}

// 返回里带工具调用轨迹，能看到模型自己调了什么：
{"reply": "...", "stop_reason": "completed", "steps": 2,
 "tool_calls": [{"step":1,"tool":"wms__list_low_stock","ok":true,"latency_ms":1150},
                {"step":2,"tool":"list_products","ok":true,"latency_ms":0}]}
```

### 请求示例

```json
POST /api/v1/recommend
Content-Type: application/json

{
  "user_id": "user_001",
  "scene": "homepage",
  "num_items": 5,
  "context": {
    "recent_views": ["手机", "耳机", "充电宝"],
    "avg_order_amount": 500,
    "last_purchase_days": 7
  }
}
```

### 响应示例

```json
{
  "request_id": "a3f8c2d1-...",
  "user_id": "user_001",
  "products": [
    {
      "product_id": "P001",
      "name": "iPhone 16 Pro",
      "category": "手机",
      "price": 7999.0,
      "score": 0.95
    },
    {
      "product_id": "P003",
      "name": "AirPods Pro 2",
      "category": "耳机",
      "price": 1899.0,
      "score": 0.88
    }
  ],
  "marketing_copies": [
    {
      "product_id": "P001",
      "copy": "根据您最近对手机的兴趣，为您精选 iPhone 16 Pro，好评率 98%，限时优惠中。"
    }
  ],
  "experiment_group": "treatment_llm",
  "total_latency_ms": 1523.4
}
```

---

## 📁 项目文件结构

> ⚠️ 这一节已按**当前实际**的文件树重写。旧版本里列的 `java/`、`go/` 目录已删除。

```
multi-agent-ecommerce-system/
│
├── README.md                    # 本文件
├── CLAUDE.md                    # 给 AI agent 的说明（含已知陷阱）
├── docker-compose.yml
├── eval_results/                # 评测报告（gitignore，含 git SHA 与实测数字）
│
├── docs/
│   ├── progress.md              # ★ 进度总览：里程碑 / 交付物 / 实测数字 / 已知问题
│   ├── harness-mcp-walkthrough.md  # ★ harness 与 MCP 具体是哪几行代码
│   ├── mcp-integration.md       # ★ MCP 使用文档（怎么配、怎么验）
│   ├── stages/                  # ★ 分阶段技术文档（每个阶段做什么、为什么）
│   │   ├── 01-fix-candidate-set.md
│   │   ├── 02-token-accounting.md
│   │   ├── 03-tool-registry.md
│   │   ├── 04-mcp-server-side.md
│   │   ├── 05-ops-copilot.md
│   │   └── 06-eval.md
│   ├── mcp-integration-prd.md   # MCP 部分的原始设计（含被否决方案的论证）
│   ├── interview-guide.md       # 面试指南（八股文 + STAR 话术）
│   ├── resume-template.md
│   ├── architecture.md          # 架构设计详解
│   ├── code-walkthrough.md      # 原有代码逐行讲解
│   └── extension-guide.md       # 二次开发指南（预留钩子 + 难度阶梯）
│
└── python/                      # 唯一的实现（Java/Go 已删除）
    ├── main.py                  # FastAPI 入口：推荐 / 实验 / 指标 / Copilot
    ├── requirements.txt
    ├── .env.example
    │
    ├── agents/                  # 4 个 Agent
    │   ├── base_agent.py        # ★ 运行时保障：熔断门→超时→重试→降级
    │   ├── user_profile_agent.py
    │   ├── product_rec_agent.py
    │   ├── marketing_copy_agent.py
    │   └── inventory_agent.py   # ★ MCP 客户端的唯一接线点
    │
    ├── harness/                 # ★ 运行时骨架（二次开发新增）
    │   ├── trace.py             #   请求级 trace（structlog contextvars）
    │   ├── breaker.py           #   熔断器（纯逻辑，零依赖，最厚的单测在这）
    │   ├── runtime.py           #   按 agent 名索引的共享熔断状态 + 调用顺序
    │   ├── llm.py               #   客户端工厂 + 会记账的 MeteredChatOpenAI
    │   ├── usage.py             #   token / 成本账本（contextvar 并发安全）
    │   ├── pricing.py           #   价格表（可覆盖，查不到返回 None 不猜）
    │   ├── deps.py              #   组合根：全进程一份实例
    │   └── tools/               #   统一工具层（内置函数与 MCP 工具同构）
    │       ├── spec.py          #     ToolSpec / ToolResult
    │       ├── registry.py      #     分发 + 超时/重试/降级
    │       ├── builtin.py       #     内置工具
    │       └── mcp_source.py    #     MCP 工具 → ToolSpec（零转换）
    │
    ├── copilot/                 # ★ 运营 Copilot（MCP Host 角色）
    │   ├── agent.py             #   多轮 tool-calling loop + 三重护栏
    │   └── router.py            #   POST /api/v1/copilot
    │
    ├── mcp_servers/             # ★ 两个 MCP Server
    │   ├── wms_server.py        #   库存服务（SQLite，4 工具 + 1 resource）
    │   ├── recommend_server.py  #   把本项目能力暴露出去（4 工具）
    │   └── init_wms_db.py       #   建表 + 种子（★ 种子故意与本地假数据不一致）
    │
    ├── orchestrator/
    │   ├── supervisor.py        #   asyncio 并行编排
    │   └── graph.py             #   LangGraph 版同一件事
    │
    ├── services/
    │   ├── ab_test.py           #   A/B 引擎（分桶 + Thompson）
    │   ├── mcp_client.py        #   ★ MCP 短连接客户端（永不抛异常给调用方）
    │   └── metrics.py           #   指标 + token/成本累计
    │
    ├── eval/                    # ★ 评测闭环
    │   ├── cases.jsonl          #   10 条 golden set
    │   ├── runner.py            #   确定性断言 + 报告 + --baseline 对比
    │   └── judge.py             #   非门禁 LLM 裁判
    │
    ├── models/schemas.py
    ├── config/settings.py
    └── tests/                   # 144 个测试，全部离线
```

## 📚 面试资料索引

| 文档 | 内容亮点 | 什么时候看 |
|------|---------|-----------|
| [📋 面试完全指南](docs/interview-guide.md) | 八股文30题（含标准答案）+ STAR法3分钟/1分钟两版话术 + 面试官追问预案 | **面试前一天通读** |
| [📝 简历模板](docs/resume-template.md) | 应届/社招两套模板，项目经验直接复制，按岗位调整技术栈关键词 | **投简历时参考** |
| [🏗 架构设计文档](docs/architecture.md) | 系统架构图 + Agent职责矩阵 + 稳定性设计 + 性能数据 | **被问架构时参考** |
| [🔍 代码讲解指南](docs/code-walkthrough.md) | 每个文件逐行解释 + 面试话术 + 常见追问应对 | **被问代码时参考** |

---

## ❓ 面试八股文精选（10题）

### Q1：为什么用 Multi-Agent 而不是单个大 Agent？

> **推荐答法（30秒）**：
> 单 Agent 管理几十个工具时，上下文膨胀、推理准确率会明显下降。Multi-Agent 的核心优势有三点：
> 1. **上下文隔离**：每个 Agent 只关注自己领域的工具和数据，Token 消耗少、推理准确
> 2. **并行加速**：4 个 Agent 可以同时跑，端到端延迟约等于最慢 Agent 的耗时，而不是四者相加
> 3. **独立演进**：各 Agent 可以独立升级、独立做 A/B 测试，互不影响

---

### Q2：Supervisor 模式和 Handoffs 模式有什么区别？

> | | Supervisor 模式 | Handoffs 模式 |
> |--|--|--|
> | 控制方式 | 中枢集中控制 | Agent 间直接传递控制权 |
> | 适合场景 | 流程固定，需要并行 | 对话式，流程动态 |
> | 状态管理 | Supervisor 统一维护 | 每次交接携带上下文 |
> | 本项目 | ✅ 采用 | ❌ 未采用 |

---

### Q3：`asyncio.gather()` 和串行调用的区别？

> ```python
> # 串行：总耗时 = 3s + 5s = 8s
> profile = await user_profile_agent.run()   # 耗时 3s
> products = await product_rec_agent.run()   # 耗时 5s
>
> # 并行：总耗时 = max(3s, 5s) = 5s
> profile, products = await asyncio.gather(
>     user_profile_agent.run(),              # 3s
>     product_rec_agent.run(),               # 5s（同时开始）
> )
> ```
> `asyncio.gather()` 适合 IO 密集型任务（调用 API、查数据库），两个任务同时"等待"，CPU 不浪费。

---

### Q4：Redis Sorted Set 怎么做实时特征？

> ```
> # 写入：用户行为事件
> ZADD user:u001:clicks {timestamp} {product_id}
>
> # 读取：最近1小时的点击
> ZRANGEBYSCORE user:u001:clicks {now-3600} {now}
>
> # 滑动窗口统计（1h / 24h / 7d）
> clicks_1h  = ZCOUNT user:u001:clicks {now-3600} {now}
> clicks_24h = ZCOUNT user:u001:clicks {now-86400} {now}
> clicks_7d  = ZCOUNT user:u001:clicks {now-604800} {now}
> ```
> 用 score=时间戳 的 Sorted Set，天然支持按时间范围查询，时间复杂度 O(log N)。

---

### Q5：A/B 测试的流量分桶怎么保证一致性？

> ```python
> # 用 MD5 哈希取模 → 同一个 user_id 每次落到同一个桶
> bucket = int(hashlib.md5(user_id.encode()).hexdigest(), 16) % 100
>
> # 0-59  → control（60%流量）
> # 60-79 → treatment_llm（20%流量）
> # 80-99 → treatment_vector（20%流量）
> ```
> 只要 user_id 不变，分桶结果永远一致。这样同一个用户在实验期间始终体验同一套策略，保证实验结论的可靠性。

---

### Q6：Thompson Sampling 怎么动态调流量？

> 核心思想：哪个实验组赢得多，就自动给它更多流量（像"站在赢家那边"）。
>
> ```python
> # 每个实验组维护 Beta 分布参数
> alpha = {"control": 100, "treatment": 80}   # 点击次数
> beta  = {"control": 50,  "treatment": 20}   # 未点击次数
>
> # 分配流量时，从各组的 Beta 分布采样，取最大值的组
> samples = {group: np.random.beta(alpha[g], beta[g]) for g in groups}
> winner = max(samples, key=samples.get)
> # CTR 越高的组，采样值越大，被选中概率越高
> ```

---

### Q7：Agent 调用失败怎么处理？

> 三层保障：
> 1. **超时控制**：`asyncio.wait_for(coro, timeout=5)` — 每个 Agent 独立超时，不阻塞整体
> 2. **指数退避重试**：失败后等 0.5s → 1s → 2s（上限 4s），共 **2 次尝试**（即只重试 1 次）。
>    ⚠️ 参数原名 `max_retries=2` 却表示"总尝试次数"，是个会误导人的命名，已改名为 `max_attempts`。
>    另外默认【不重试】更多次：一个超时 3 秒的只读工具重试两次，会在一条本就 16 秒的链路上再烧 6 秒，换来的还是同一个答案。
> 3. **降级（Fallback）**：全部重试失败后，返回"说得过去的默认结果"（如热门商品列表），保证整个系统不崩溃

---

### Q8：LangGraph 和直接写 `asyncio.gather()` 有什么区别？

> | | LangGraph | 直接写 asyncio |
> |--|--|--|
> | 状态管理 | 内置 State，节点间自动传递 | 手动管理变量 |
> | 持久化 | 内置 Checkpoint，支持断点续跑 | 需要自己实现 |
> | 可视化 | 可以画出状态图 | 无 |
> | Human-in-the-loop | 内置支持，可以在节点暂停等人工确认 | 需要自己实现 |
> | 适合场景 | 复杂、有分支的工作流 | 简单并行任务 |

---

### Q9：RFM 模型怎么计算？

> ```
> R (Recency)  = 距离上次购买的天数    → 越小越好（最近买过）
> F (Frequency)= 一定周期内购买次数    → 越大越好（买的勤）
> M (Monetary) = 累计消费金额          → 越大越好（花的多）
>
> # 归一化到 0-1，加权求和
> rfm_score = 0.3 * R_norm + 0.3 * F_norm + 0.4 * M_norm
>
> # 用于分群：
> VIP:          rfm_score > 0.8
> 活跃用户:     0.6 < rfm_score ≤ 0.8
> 价格敏感:     高 F，低 M（买的勤但花得少）
> 流失风险:     rfm_score < 0.3
> ```

---

### Q10：系统延迟是怎么优化的？

> **实测数据**（不是目标值）：全链路 p50 从 **48,015 ms 降到 2,788 ms（约 17 倍）**。
>
> 三步，每一步都有先测量再动手：
>
> **第一步：先让重试可见。** 我一度以为某次 63 秒的请求是重试造成的，
> 于是加了 `agent.retry` 事件去验证 —— **结果一次都没触发**，我的判断是错的。
> 真相是模型单次调用真的花了 54 秒。
>
> **第二步：定位到真正的瓶颈。** 重复测量同一 prompt 发现：
> `input_tokens` 每次精确等于 612（与网络无关）、
> **延迟与推理 token 数的相关系数 r = 0.997**、
> 98.5% 的输出 token 是推理 token。
> 逐个试关闭手段：`max_tokens=64` 反而更慢（该参数被模型厂商忽略）、
> `reasoning_effort=low` 无效、提示词要求直接作答省 40%，
> 而 `thinking={"type":"disabled"}` 让单次调用从 9,198ms 降到 981ms。
>
> **第三步：并行化本来就有，省下的是串行部分。** 库存检查与重排本来就并行，
> 所以关掉推理后新瓶颈变成 Phase 3 的串行文案生成。

> ⚠️ 关于"P99 < 2s"：**那是早期写的目标值，从未达到过。**
> 实测 p95 是 3,997 ms。不要引用 2 秒这个数字。

👉 **更多30题详见** [docs/interview-guide.md](docs/interview-guide.md)

---

## 📋 简历写法（直接复制）

> ⚠️ **下面每个数字都能在仓库里找到出处**（`eval_results/` 下的评测报告）。
> 旧版本里写的"CTR 提升 15%、文案点击率提升 23%"**没有数据支撑，已删除** ——
> 面试官问"你怎么测的 A/B"会非常被动。

```
多Agent电商推荐与营销系统 | 个人项目 | 2026.09 - 2026.10
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• 【Agent 运行时】为 4-Agent 系统实现运行时保障层：contextvars 贯穿请求级 trace、
  asyncio.wait_for 真超时、滑动窗口熔断与两级降级。
  故障注入验证：注入 0.3s 超时后前两次 311ms 降级、第 3 次起 0ms 短路，
  四次请求全部 HTTP 200 且商品文案照常返回

• 【MCP 三端】基于 MCP Python SDK v2 实现 Server + Client + Host 三端：
  自建库存 Server（SQLite，4 工具 + 1 resource）与推荐 Server（暴露 4 个工具）；
  库存 Agent 作为 Client 消费 WMS；并实现多轮 tool-calling loop 的运营 Copilot 作为 Host。
  MCP 不可用时自动降级并在响应中标注 source=fallback

• 【协议层判断】评估现成 SQLite MCP Server 后主动否决（官方已归档、存在 CWE-89、
  通用 SQL 入口与库存查询的确定性要求语义不匹配），自建确定性工具契约

• 【性能与成本】通过测量定位到延迟与推理 token 数相关系数 0.997，
  关闭确定型任务的推理后全链路 p50 从 48,015ms 降至 2,788ms（约 17 倍），
  单次推荐成本从 $0.0025 降至 $0.0006（4.1 倍）

• 【评测闭环】构建 10 条 golden set + 确定性断言门禁 + 非门禁 LLM 裁判，
  报告带 git SHA / 模型 / 价格表指纹保证前后可比。
  基于该闭环完成一次结论反转：按实测数据推翻了自己此前"文案保留推理"的判断

技术栈：MCP · LangGraph · FastAPI · SQLite · Redis · structlog · tenacity · Pytest
```

**为什么这版比旧版强**：
① 旧版写"CTR 提升 15%"，面试官问"怎么测的"会立刻穿帮 —— 那两个数字没有任何数据支撑；
② 新版**主动展示了两处"我否决/推翻了自己"**（否决现成 MCP Server、推翻自己的推理决策），
   这比任何正向指标都更能证明工程判断力；
③ 每个数字都能指向 `eval_results/` 里的一份可复现报告。

---

## 🔗 参考资料与致谢

本项目架构设计参考了以下企业级开源项目：

| 项目 | 说明 | 链接 |
|------|------|------|
| NVIDIA Retail Agentic Commerce | NVIDIA 企业级电商 Agent 蓝图 | [GitHub](https://github.com/NVIDIA-AI-Blueprints/Retail-Agentic-Commerce) |
| Spring AI Alibaba Multi-Agent Demo | 阿里巴巴 Java 多 Agent 示例 | [GitHub](https://github.com/spring-ai-alibaba/spring-ai-alibaba-multi-agent-demo) |
| LangGraph 官方文档 | LangGraph 状态图框架 | [文档](https://langchain-ai.github.io/langgraph/) |
| 京东商家智能助手技术博客 | 京东 Multi-Agent 生产实践 | [掘金](https://juejin.cn/post/7470344960563871784) |
| DualAgent-Rec | 双 Agent 推荐系统 | [GitHub](https://github.com/GuilinDev/Dual-Agent-Recommendation) |
| MiniMax API | 本项目默认 LLM 服务 | [官网](https://www.minimax.chat/) |

---

## 📄 License

[MIT License](LICENSE) — 随意使用、修改、商用，保留 License 声明即可。

---

<div align="center">

**如果这个项目对你有帮助，欢迎点个 ⭐ Star！**

有问题欢迎提 [Issue](https://github.com/bcefghj/multi-agent-ecommerce-system/issues)

</div>
