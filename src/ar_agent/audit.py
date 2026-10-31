"""L5 observability (product spec §5).

"Finance and the CFO can ask at any time: was this reminder sent by the agent or by a person?"

So the first-class field of the log is `actor`, not the timestamp.
Every record answers four questions: who acted, on whom, why, and under which rule.
"""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

LOG_COLUMNS = [
    "timestamp",        # when the action happened
    "run_id",           # which scan run
    "actor",            # agent / human / system  <- the key field
    "invoice_id",
    "customer_id",
    "customer_name",
    "tier",             # final tier
    "base_tier",        # tier before the policy layer (the difference shows what the policy layer did)
    "action",
    "channel",
    "recipient",
    "template_id",
    "mode",             # live / dry_run
    "ok",
    "reasons",          # why this tier
    "policy_hits",      # L4 rules that fired
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
