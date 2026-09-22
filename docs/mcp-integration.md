# MCP 集成使用文档

> 技术原理与设计取舍见 [stages/04-mcp-server-side.md](stages/04-mcp-server-side.md)
> 和 [stages/03-tool-registry.md](stages/03-tool-registry.md)。这份只讲**怎么用**。

本项目同时扮演 MCP 的**两种角色**：

| 角色 | 哪个组件 | 干什么 |
|---|---|---|
| **Client** | `agents/inventory_agent.py` → `services/mcp_client.py` | 【消费】外部的 WMS 库存服务 |
| **Server** | `mcp_servers/recommend_server.py` | 把项目能力【暴露】给外部 Host |
| **Server** | `mcp_servers/wms_server.py` | 被上面那个 Client 消费的库存服务 |
| Host | （阶段 E 的运营 Copilot） | 自己驱动模型去调工具 |

---

## 一、当作 Client：让库存 Agent 走 MCP

### 1. 初始化库存数据库

```bash
cd python
.venv/Scripts/python.exe mcp_servers/init_wms_db.py --reset
```

输出会告诉你种子数据的情况：

```
WMS 数据库已初始化
  路径           : ./wms.db
  商品数         : 15
  与目录库存不同 : 9 个   <- A5 靠这批产生可见差异
  WMS 缺货       : 2 个
```

> **为什么种子数据要和 `Product.stock` 不一样？**
> 若两者相同，MCP 通与不通的输出完全一致 —— 测试全绿也说明不了任何事。
> 有差异才能**证明** MCP 真的在起作用（见下面第四节的验收）。

### 2. 打开开关

```bash
# .env 或环境变量
ECOM_MCP_WMS_ENABLED=true
ECOM_MCP_WMS_DB_PATH=./wms.db        # 相对 python/ 目录
ECOM_MCP_WMS_TIMEOUT=3.0             # 必须 < agent_timeout_inventory(5.0)
```

**默认是 `false`** —— 新能力默认关闭，保证现有链路零破坏。

### 3. 验证

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/recommend \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"u1","num_items":5}' | python -m json.tool | grep source
```

看到 `"source": "mcp"` 就是走通了。

---

## 二、当作 Server：把项目暴露给外部 AI 工具

### 1. 确认能启动

```bash
cd python
.venv/Scripts/python.exe mcp_servers/recommend_server.py
# 会阻塞等待 stdio 输入 —— 这是正常的，Host 会拉起它
```

### 2. 配到 Host 里（以 Claude Desktop 为例）

编辑 `claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "ecommerce-recommend": {
      "command": "D:/devlop/multi-agent-ecommerce-system/python/.venv/Scripts/python.exe",
      "args": ["D:/devlop/multi-agent-ecommerce-system/python/mcp_servers/recommend_server.py"],
      "cwd": "D:/devlop/multi-agent-ecommerce-system/python"
    }
  }
}
```

> ⚠️ **`command` 必须写绝对路径。**
> 这台机器上有 11 个 Python 解释器、其中 6 个目录名叫 `.venv`。
> 写 `"python"` 会命中 PATH 上的任意一个，症状是 `ModuleNotFoundError` 且极难定位。

### 3. 暴露出来的 2 个工具

| 工具 | 说明 | 耗时 |
|---|---|---|
| `recommend_products(user_id, scene, num_items, context)` | 完整四 Agent 编排，返回商品+文案+用量 | **约 5–15 秒** |
| `get_metrics()` | 各 Agent 指标、熔断状态、token 与成本 | 毫秒级 |

**两个刻意的设计**：

- **全部只读**：暴露的 2 个工具都不改任何状态 —— Host 侧的模型误触发也不会产生副作用
  （早期还有一个 `record_experiment_outcome` 写操作，已随那个没接线的 A/B 引擎一并删除）。
- **`recommend_products` 的描述里写明了"约 5-15 秒"** —— 否则 Host 侧的模型会按
  本地查询的预期给它一个短超时，然后判定失败。

---

## 三、配置项一览

| 变量 | 默认 | 说明 |
|---|---|---|
| `ECOM_MCP_ENABLED` | `false` | 是否注册 recommend_server 的工具 |
| `ECOM_MCP_WMS_ENABLED` | `false` | 是否让库存 Agent 走 MCP |
| `ECOM_MCP_WMS_DB_PATH` | `./wms.db` | SQLite 路径，相对 `python/` |
| `ECOM_MCP_WMS_TIMEOUT` | `3.0` | 秒。**必须小于 `agent_timeout_inventory`(5.0)** |

关于超时的那个约束：让 MCP **先**超时，库存 Agent 才有余量走降级。
如果反过来，Agent 会先被自己的超时切断，降级路径根本没机会执行。

---

## 四、验收：怎么知道它真的在工作

### A4 —— 降级（本方案的灵魂）

```bash
# 故意把库路径指向一个不存在的文件
ECOM_MCP_WMS_ENABLED=true \
ECOM_MCP_WMS_DB_PATH=./this_does_not_exist.db \
  .venv/Scripts/python.exe -m uvicorn main:app --port 8099
```

预期结果：

```
HTTP 200                                    ← 主接口没挂
"source": "fallback"                        ← 降级【可观测】
商品和文案照常返回                            ← 业务没受影响
mcp.tool_error -> inventory.mcp_degraded    ← 日志链完整
```

> 只做「能用」是 A3；能「挂掉也不怕」才是 A4。**后者才是生产可用的判据。**

### A5 —— 契约（证明不是"看起来一样"）

```bash
cd python && .venv/Scripts/python.exe -c "
import asyncio, os
from agents.product_rec_agent import MOCK_PRODUCTS
from agents.inventory_agent import InventoryAgent

async def run(enabled):
    os.environ['ECOM_MCP_WMS_ENABLED'] = 'true' if enabled else 'false'
    from config import get_settings; get_settings.cache_clear()
    import importlib, agents.inventory_agent as m; importlib.reload(m)
    r = await m.InventoryAgent().run(products=list(MOCK_PRODUCTS))
    return set(r.available_products), r.data['source']

async def main():
    on, s_on = await run(True)
    off, s_off = await run(False)
    print(f'MCP 开  source={s_on}  可用 {len(on)}/15')
    print(f'MCP 关  source={s_off}  可用 {len(off)}/15')
    print('只有开着时才被过滤的:', sorted(off - on))

asyncio.run(main())
"
```

预期：

```
MCP 开  source=mcp        可用 13/15
MCP 关  source=fallback   可用 15/15
只有开着时才被过滤的: ['P007', 'P014']     ← 恰好是 WMS 里没货的两个
```

**唯一差异就是那两个缺货商品，此外零差异** —— 这才叫证明了 MCP 生效。

---

## 五、常见问题

### `ModuleNotFoundError: No module named 'mcp'`

用错解释器了。必须用 `python/.venv/Scripts/python.exe` 的**完整路径** ——
根目录那个 `.venv` 是空的。

### `FileNotFoundError: WMS 数据库不存在`

先跑 `python mcp_servers/init_wms_db.py --reset`。

### MCP 调用怎么要 1 秒多？

正常。每次调用都要新起一个子进程（`import mcp` 本身约 1 秒），
而光 `import mcp` 就要 1 秒。这是为了规避 `uvicorn --reload` 杀子进程的问题，
代价是每次约 **1.15 秒**。

**这也是为什么库存 Agent 必须用 `batch_query_stock` 一次查完所有商品** ——
逐个查的话，20 个商品就是 20 × 1.15 ≈ 23 秒。

### 想接别的现成 MCP Server？

改一个 JSON 配置就行，不用动代码。但**先想清楚它解决什么真实问题** ——
加一个用不上的 Server 只会增加攻击面和启动开销。

`docs/mcp-integration-prd.md` 的附录 C 评估过若干候选（`server-time`、
`server-fetch` 等）并给了取舍建议。
