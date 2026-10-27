"""AR Collections Agent —— 应收账款催收 Agent。

模块地图（对应 PM 文档章节）：
    tiering.py    §4   分级决策矩阵
    policy.py     §5   L4 运行时策略层（硬规则，独立于 LLM）
    breaker.py    §5   L6 熔断机制
    templates.py  §5   L2 工具设计（只能发预设模板）
    inbound.py    §4.2 关键词驱动的强制人工升级
    orchestrator  §3.1 每日扫描主流程
    report.py     §3.1 周报层
"""

__version__ = "1.0.0"
