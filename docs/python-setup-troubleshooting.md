# Python 版依赖安装卡住 — 排查结论与修复步骤

> 排查日期：2026-09-20 ｜ 环境：Windows + miniconda Python 3.14.6 ｜ 仓库：`D:\devlop\multi-agent-ecommerce-system`

## 一、结论（先说根因）

**根因：依赖从未真正安装失败，而是从官方源 `pypi.org` 下载速度被压到约 1 MB/分钟，pip 长时间无可见输出，看起来像"卡死"。**

本项目 Python 版依赖共 **77 个包**（含 langchain 全家桶、numpy、pandas、grpcio 等大体积 wheel），按实测速率需要数小时，因此会一直"卡住"。

**修复：换用国内镜像源。** 实测并已验证可用：**阿里云镜像**（首选）、**腾讯云镜像**（备用）。

---

## 二、证据链（实测数据，非推测）

| 检查项 | 实测结果 | 判定 |
|---|---|---|
| `pypi.org/simple/requests/` 真实 HTTPS GET | **read timeout 23.06 s** | 官方源不可用 |
| 清华镜像 HTTPS GET | HTTP 200，0.77 s | 可达 |
| 阿里云镜像 HTTPS GET | HTTP 200，0.56 s | 可达 |
| 腾讯云镜像 HTTPS GET | HTTP 200，1.00 s | 可达 |
| 直连 pypi.org 安装（12 分钟采样） | pip 缓存 49 MB → 60 MB，**≈0.9 MB/min**，site-packages 为空 | 卡住复现 |
| 阿里云镜像完整安装 | **exit_code=0，65 个包，741 s**，下载速率 1.5 MB/s | 成功 |
| 依赖解析（Python 3.14.6，官方源） | 77 个包全部解析成功，含 `grpcio-1.84.0-cp314-win_amd64`、`numpy-2.5.3-cp314` | Python 版本无问题 |

### 已排除的原因

1. **Python 版本不匹配 —— 排除。** README 要求 3.11+，本机为 3.14.6。实测 3.14.6 下 77 个依赖可完整解析，且 `grpcio` / `numpy` / `pandas` / `pydantic-core` 均已提供 `cp314` Windows 预编译 wheel，无需本地编译，不涉及 MSVC 工具链。
2. **缺失系统依赖 —— 排除。** 全部依赖均有 Windows wheel，无源码编译环节。
3. **虚拟环境创建失败 —— 排除。** 两个 venv 均可正常执行 `python -V` 与 `pip -V`（pip 26.1.2）。
4. **依赖冲突 —— 排除。** 解析无 `ResolutionImpossible`。

### 需要警惕的第二类原因：代理变量

排查中发现：当 `HTTP_PROXY` / `HTTPS_PROXY` 指向一个**未运行**的本地端口时，pip 与 Python 网络请求会**无限挂起且无任何输出**——症状与本次"卡住"完全一致。

本机 `workbuddy` 注入的 `HTTP_PROXY=http://127.0.0.1:64798` 实测即导致挂起。若你的终端里配置过代理（Clash / v2ray 等）且代理未启动或端口已变，会出现同样现象。

**检测命令**（在 CMD / PowerShell 中执行，输出为空即未设置）：

```bat
echo %HTTP_PROXY% %HTTPS_PROXY%
```

有输出就说明存在代理变量；若代理没在运行，先清除：

```bat
set HTTP_PROXY=
set HTTPS_PROXY=
```

---

## 三、修复步骤（可直接照抄）

### 第 1 步：进入 Python 目录并使用 README 指定的 venv

```bat
cd /d D:\devlop\multi-agent-ecommerce-system\python
.venv\Scripts\activate
```

### 第 2 步：临时使用国内镜像安装（推荐先临时，确认可用后再固化）

```bat
python -m pip install -i https://mirrors.aliyun.com/pypi/simple -r requirements.txt
```

备用镜像（阿里云失败时换）：

```bat
python -m pip install -i https://mirrors.cloud.tencent.com/pypi/simple -r requirements.txt
```

> 注意：实测清华镜像 `https://pypi.tuna.tsinghua.edu.cn/simple` 在本机 pip 26.1.2 下报
> `No matching distribution found`，**不要用它**（这是本次唯一踩到的镜像坑）。

### 第 3 步（可选）：把镜像固化，避免每次带参数

```bat
python -m pip config set global.index-url https://mirrors.aliyun.com/pypi/simple
```

配置文件位于 `%APPDATA%\pip\pip.ini`，可用 `python -m pip config list` 查看当前生效配置。

### 第 4 步：配置 API Key

```bat
copy .env.example .env
notepad .env
```

把 `ECOM_LLM_API_KEY=your_api_key_here` 换成真实的 LLM Key（MiniMax 或阿里通义）。

### 第 5 步：启动

```bat
python main.py
```

看到 `Uvicorn running on http://0.0.0.0:8000` 即为成功。

---

## 四、验证方法

### 1. 依赖完整性

```bat
python -m pip check
python -c "import fastapi, langgraph, langchain_openai, pymilvus, redis, numpy; print('deps ok')"
```

### 2. 健康检查

```bat
curl http://localhost:8000/health
```

预期：`{"status":"healthy","model":"MiniMax-M1"}`

### 3. 核心接口

```bat
curl -X POST http://localhost:8000/api/v1/recommend -H "Content-Type: application/json" -d "{\"user_id\":\"user_001\",\"scene\":\"homepage\",\"num_items\":3,\"context\":{\"recent_views\":[\"手机\",\"耳机\"],\"avg_order_amount\":500}}"
```

**本次实测结果**（占位 API Key）：

```json
HTTP 200
experiment_group : control
total_latency_ms : 1804.9
products         : 3  (P001 iPhone 16 Pro / P002 华为 Mate 70 / P003 AirPods Pro 3)
agent_results:
  user_profile   success=False  latency=1076.4  err=401 授权失败
  product_rec    success=True   latency=0.2
  marketing_copy success=False  latency=711.0   err=401 授权失败
  inventory      success=True   latency=0.1
```

说明两点：
- 接口链路是通的（HTTP 200 + 3 个商品返回）；
- 填入真实 Key 后 `user_profile` 与 `marketing_copy` 的 401 会消失，个性化文案才会出现。**这正好验证了系统的降级设计**：LLM 挂掉时推荐主流程仍可返回商品列表。

---

## 五、其他需要注意的点

### 1. `requirements.txt` 缺少 pytest

`python/tests/test_ab_test.py` 使用 pytest 风格（裸 `assert`），但依赖清单里没有 pytest，直接跑测试会报模块缺失：

```bat
python -m pip install pytest -i https://mirrors.aliyun.com/pypi/simple
python -m pytest tests/ -v
```

### 2. 仓库里存在两个 venv，建议只保留 `python\.venv`

| 路径 | 状态 | 说明 |
|---|---|---|
| `python\.venv` | 可用，依赖已装好 | README 指定位置，**以它为准** |
| `.venv`（仓库根目录） | 空环境，仅 pip | 未被 README 使用；建议删除以免混淆 |

删除前请自行确认，删除命令（在仓库根目录执行）：

```bat
rmdir /s /q .venv
```

### 3. Redis / Milvus 不影响最小启动

代码中 `FeatureStore` 的 Redis 客户端与 Milvus 向量库**从未被实例化**（构造函数里是 `None` 占位），商品召回使用内置的 15 条 mock 数据。因此：

- 只想把服务跑起来：**不需要** `docker-compose up`；
- 需要完整体验 Redis/Milvus/MySQL：再执行 `docker-compose up -d`。

### 4. 运行目录要求

`config/settings.py` 用 `env_file=".env"` 相对路径加载配置，**必须在 `python\` 目录下启动**，否则 `.env` 不会被读取。

---

## 六、排查用的探针脚本

本次排查脚本已归档至 `D:\devlop\_archive\probes_20260920\`：

- `_probe_net.py` — DNS / TCP 多主机连通性探活
- `_probe2.py` — 镜像源 HTTPS 真实 GET 对比 + 依赖总体积统计
- `probe_recommend.py` — 推荐接口端到端验证（参数为端口号）
- `_dryrun_pypi.json` — 官方源下的完整依赖解析报告（77 个包）

---

## 七、Windows（CMD）逐步命令 —— 照抄即可

> README 第 4 步写的是 `cp .env.example .env`，**`cp` 是 Linux / macOS 命令，CMD 里不存在**，
> 会报 `'cp' 不是内部或外部命令，也不是可运行的程序或批处理文件`。

```bat
:: 0. 关键：必须在 python 子目录下操作（.env.example 在 python\ 里，仓库根目录没有）
cd /d D:\devlop\multi-agent-ecommerce-system\python

:: 1. 激活 README 指定的 venv —— 是 python\.venv，不是仓库根目录那个 .venv
deactivate
.venv\Scripts\activate
:: 激活成功后行首会出现 (.venv)

:: 2. 安装依赖（上一轮已装好可跳过）
python -m pip install -i https://mirrors.aliyun.com/pypi/simple -r requirements.txt

:: 3. 生成 .env —— CMD 用 copy，不是 cp
copy .env.example .env

:: 4. 填入真实 API Key
notepad .env

:: 5. 启动
python main.py
```

### 命令对照表

| 用途 | Linux / macOS / Git Bash | Windows CMD | PowerShell |
|---|---|---|---|
| 复制文件 | `cp a b` | `copy a b` | `Copy-Item a b` |
| 查看文件 | `cat a` | `type a` | `Get-Content a` |
| 删除文件 | `rm a` | `del a` | `Remove-Item a` |
| 设环境变量 | `export A=1` | `set A=1` | `$env:A="1"` |

### 两个最容易踩的坑

1. **目录错位**：`.env.example` 位于 `python\` 下，**仓库根目录没有这个文件**。在根目录执行 `copy .env.example .env` 会报"系统找不到指定的文件"。
2. **venv 错位**：仓库里同时存在两个 venv。根目录 `.venv` 是**空环境（只有 pip，2 个条目）**，`python\.venv` 才装有依赖（**160 个条目**）。激活错了会在启动时报 `ModuleNotFoundError: No module named 'fastapi'`。
   判断当前 venv 是否有依赖：

   ```bat
   python -c "import fastapi, langgraph; print('deps ok')"
   pip list | findstr langgraph
   ```

### 端口占用处理（如启动时报 address already in use）

```bat
netstat -ano | findstr :8000
taskkill /F /PID <上面查到的 PID>
```

---

## 八、`ModuleNotFoundError: No module named 'structlog'` 的规避（venv 串台）

现象：`python -c "import fastapi, langgraph"` 输出 `deps ok`，但 `python main.py` 在 `main.py` 第 22 行 `import structlog` 处报 `ModuleNotFoundError`。

**原因是提示符 `(.venv)` 无法区分是哪个项目的 venv。** 本机同时存在 **6 个名为 `.venv` 的环境**，激活过任意一个（哪怕之后 `cd` 到了本项目），提示符都显示同样的 `(.venv)`：

| 路径 | fastapi | langgraph | structlog |
|---|---|---|---|
| `multi-agent-ecommerce-system\python\.venv` | ✅ | ✅ | ✅ **（正确的）** |
| `multi-agent-ecommerce-system\.venv`（仓库根） | ❌ | ❌ | ❌ 空环境 |
| `helloagents-trip-planner\backend\venv` | ✅ | ❌ | ❌ |
| `Hello_Agent\.venv` | ❌ | ❌ | ❌ |
| `ai-ticket-system\.venv` | ❌ | ❌ | ❌ |
| `Documents\Codex\PetCare-AI-Agent\.venv` | ✅ | ❌ | ❌ |

### 规避方法 1（推荐）：不用 activate，直接用绝对路径调用

```bat
cd /d D:\devlop\multi-agent-ecommerce-system\python
.venv\Scripts\python.exe main.py
```

这样**不可能**走到别的环境，已在 `import main` 层面验证通过（含 structlog / uvicorn / fastapi 全部依赖）。

### 规避方法 2：先确认再用

```bat
python -c "import sys; print(sys.executable)"
```

输出必须是 `D:\devlop\multi-agent-ecommerce-system\python\.venv\Scripts\python.exe`，否则先 `deactivate`，或**直接关掉窗口重开一个新的 CMD**（旧窗口里激活过的 venv 不会因为 `cd` 而失效）。

配套自检：

```bat
where python
pip -V
```
