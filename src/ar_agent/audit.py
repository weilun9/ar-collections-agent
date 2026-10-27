"""L5 可观测性 —— PM 文档 §5。

「财务/CFO 可随时查询：这条催收是 Agent 发的还是人工发的」。

所以日志表的第一等公民是 actor 字段，而不是时间戳。
每条记录都能回答四个问题：谁做的、对谁做的、为什么做、依据哪条规则。
"""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

LOG_COLUMNS = [
    "timestamp",        # 动作发生时间
    "run_id",           # 哪一次扫描任务
    "actor",            # agent / human / system  ← 核心字段
    "invoice_id",
    "customer_id",
    "customer_name",
    "tier",             # 最终分级
    "base_tier",        # 策略层介入前的分级（对比可看出策略层做了什么）
    "action",
    "channel",
    "recipient",
    "template_id",
    "mode",             # live / dry_run
    "ok",
    "reasons",          # 分级依据
    "policy_hits",      # 命中的 L4 规则
    "content_preview",
]


class AuditLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "w", encoding="utf-8", newline="") as fh:
                csv.DictWriter(fh, fieldnames=LOG_COLUMNS).writeheader()

    def write(self, **row) -> None:
        record = {c: "" for c in LOG_COLUMNS}
        record["timestamp"] = row.pop("timestamp", None) or datetime.now().isoformat(timespec="seconds")
        for k, v in row.items():
            if k in record:
                if isinstance(v, (list, tuple)):
                    v = " | ".join(str(x) for x in v)
                record[k] = "" if v is None else str(v)
        with open(self.path, "a", encoding="utf-8", newline="") as fh:
            csv.DictWriter(fh, fieldnames=LOG_COLUMNS).writerow(record)

    def read_all(self) -> list[dict[str, str]]:
        if not self.path.exists():
            return []
        with open(self.path, encoding="utf-8-sig", newline="") as fh:
            return list(csv.DictReader(fh))
