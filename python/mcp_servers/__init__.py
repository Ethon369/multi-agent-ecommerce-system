"""
MCP Server 实现。

    D1 recommend_server —— 把项目已有能力（推荐/实验/指标）封装成 MCP 工具，
                          供【外部】MCP Host（Claude Desktop / Cursor）调用。
    D2 wms_server       —— SQLite 支撑的库存数据源，供【本项目的库存 Agent】
                          作为 MCP Client 消费。

两者的角色完全不同：
    D1 是"把项目开放出去"（增值，不影响内部链路）
    D2 是"引入一个真实的外部数据源"（替换掉 Product.stock 这份假数据）

注意：本包的模块【不要】互相 import，也不要 import agents/ —— 它们是
独立进程的入口。wms_server 每次调用都起一个新进程（见 PRD 决策 3），
所以它的 import 开销直接变成每次调用的延迟。实测 import agents 会连带
拉进 langchain，约 2 秒 —— 因此重活放在 init_wms_db.py（一次性脚本）里。
"""
