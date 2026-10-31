"""CSV implementation, standing in for the Google Sheets finance system in the product spec.

CSV opens directly in Excel, which makes it easy to check the source data and what the agent wrote back.
"""
from __future__ import annotations

import csv
from pathlib import Path

from ..models import Customer, Invoice
from .base import ReceivablesStore

INVOICE_COLUMNS = [
    # Business master data (read-only for the agent)
    "invoice_id", "customer_id", "amount", "issue_date", "due_date", "status", "paid_date",
    # Agent state fields (writable by the agent)
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
        self._assert_writable(fields)          # <- L3 gate: an unauthorised write raises immediately
        rows = self._read_raw()
        for row in rows:
            if row["invoice_id"].strip() == invoice_id:
                for k, v in fields.items():
                    row[k] = "" if v is None else str(v)
                break
        else:
            raise KeyError(f"invoice not found: {invoice_id}")
        self._write_raw(rows)

    def bulk_update_agent_fields(self, updates: dict[str, dict]) -> None:
        """Write back in bulk instead of rewriting the whole table for every invoice."""
        for fields in updates.values():
            self._assert_writable(fields)
        rows = self._read_raw()
        for row in rows:
            fields = updates.get(row["invoice_id"].strip())
            if fields:
                for k, v in fields.items():
                    row[k] = "" if v is None else str(v)
        self._write_raw(rows)
