FROM python:3.12-slim

WORKDIR /app

# 依赖单独一层：改代码不会让 pip install 这层缓存失效。
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 注意 .dockerignore 必须把 .venv/ 排除掉。
# 缺了它的话这一行会把宿主机上那个装了几十个包的 .venv 整个搬进镜像
# （体积和构建时间都会翻好几倍），而那个 venv 在容器里根本用不上。
COPY . .

EXPOSE 8000

# ⚠️ 不要改回 `python main.py`。
#
# main.py 的 __main__ 块调的是 uvicorn.run(..., reload=<ECOM_DEV_RELOAD>)，
# 而容器里既不需要热重载，也【不能】开：uvicorn 的 reloader 重启时会杀掉
# 整个子进程树，MCP 的 stdio 子进程会先死 —— 症状是静默挂起或 BrokenPipe，
# 且没有任何栈指向真正原因（CLAUDE.md 陷阱 5，也是 services/mcp_client.py
# 坚持用短连接的原因）。直接起 uvicorn 就没有 reloader，这个问题不存在。
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

# 健康检查用 python 的 urllib 而不是 curl：python:3.12-slim 里没有 curl，
# 为了一个探活多背一个包不值当。
#
# start-period 给 20s：lifespan 里要构建 LangGraph 图，并且（特征层打开时）
# 要做一次 Redis warmup，这段时间探活失败是正常的，不该被计入 retries。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"]
