"""CSV 实现 —— 模拟 PM 文档里的 Google Sheets 财务系统。

CSV 可以直接用 Excel 打开，方便核对原始数据和 Agent 写回的内容。
"""
from __future__ import annotations

import csv
from pathlib import Path

from ..models import Customer, Invoice
from .base import ReceivablesStore

INVOICE_COLUMNS = [
    # 业务主数据（Agent 只读）
    "invoice_id", "customer_id", "amount", "issue_date", "due_date", "status", "paid_date",
    # Agent 状态字段（Agent 可写）
    "agent_tier", "agent_stage", "agent_state", "agent_last_action_at",
    "agent_buffer_until", "agent_owner", "agent_note",
]


class CsvStore(ReceivablesStore):
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.customers_path = self.data_dir / "customers.csv"
        self.invoices_path = self.data_dir / "receivables.csv"

    def load_customers(self) -> dict[str, Customer]:
        with open(self.customers_path, encoding="utf-8-sig", newline="") as fh:
            return {r["customer_id"].strip(): Customer.from_row(r) for r in csv.DictReader(fh)}

    def load_invoices(self) -> list[Invoice]:
        with open(self.invoices_path, encoding="utf-8-sig", newline="") as fh:
            return [Invoice.from_row(r) for r in csv.DictReader(fh)]

    def _read_raw(self) -> list[dict[str, str]]:
        with open(self.invoices_path, encoding="utf-8-sig", newline="") as fh:
            return list(csv.DictReader(fh))

    def _write_raw(self, rows: list[dict[str, str]]) -> None:
        tmp = self.invoices_path.with_suffix(".csv.tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=INVOICE_COLUMNS)
            w.writeheader()
            for r in rows:
                w.writerow({c: r.get(c, "") for c in INVOICE_COLUMNS})
        tmp.replace(self.invoices_path)

    def update_agent_fields(self, invoice_id: str, **fields) -> None:
        self._assert_writable(fields)          # ← L3 闸门，越权写入直接抛异常
        rows = self._read_raw()
        for row in rows:
            if row["invoice_id"].strip() == invoice_id:
                for k, v in fields.items():
                    row[k] = "" if v is None else str(v)
                break
        else:
            raise KeyError(f"账款不存在: {invoice_id}")
        self._write_raw(rows)

    def bulk_update_agent_fields(self, updates: dict[str, dict]) -> None:
        """批量写回，避免每条账款都重写一次整表。"""
        for fields in updates.values():
            self._assert_writable(fields)
        rows = self._read_raw()
        for row in rows:
            fields = updates.get(row["invoice_id"].strip())
            if fields:
                for k, v in fields.items():
                    row[k] = "" if v is None else str(v)
        self._write_raw(rows)
