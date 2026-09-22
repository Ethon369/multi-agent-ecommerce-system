"""业务服务层：A/B 实验、指标收集。

刻意不做 re-export —— 调用方一律直接 `from services.xxx import Yyy`。
多一层包级导出，只是多一处将来会忘记同步的地方。
"""
