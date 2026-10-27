"""Google Sheets 实现 —— 生产形态的占位适配器。

演示默认用 CsvStore；接真实 Sheets 时安装 google-api-python-client 并填 .env：
    GOOGLE_SERVICE_ACCOUNT_JSON=C:\path\to\sa.json
    GOOGLE_SHEET_ID=1AbC...

L3 设计要点（面试可讲）：给这个 Service Account 授的是**表级共享 + 代码级字段白名单**双层限制。
Sheets API 本身没有列级权限，所以字段白名单由 base.ReceivablesStore._assert_writable 在
代码层强制；真正上生产应改用有列级权限的数据库视图，把这层下沉到数据库。
"""
from __future__ import annotations

import os

from ..models import Customer, Invoice
from .base import ReceivablesStore
from .csv_store import INVOICE_COLUMNS


class GoogleSheetsStore(ReceivablesStore):
    def __init__(self, sheet_id: str | None = None):
        self.sheet_id = sheet_id or os.environ.get("GOOGLE_SHEET_ID", "")
        self._svc = None

    def _service(self):
        if self._svc is None:
            from google.oauth2.service_account import Credentials      # type: ignore
            from googleapiclient.discovery import build                # type: ignore

            creds = Credentials.from_service_account_file(
                os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"],
                scopes=["https://www.googleapis.com/auth/spreadsheets"],
            )
            self._svc = build("sheets", "v4", credentials=creds).spreadsheets()
        return self._svc

    def _values(self, rng: str) -> list[list[str]]:
        resp = self._service().values().get(spreadsheetId=self.sheet_id, range=rng).execute()
        return resp.get("values", [])

    @staticmethod
    def _to_dicts(values: list[list[str]]) -> list[dict[str, str]]:
        if not values:
            return []
        header, *rows = values
        return [{h: (r[i] if i < len(r) else "") for i, h in enumerate(header)} for r in rows]

    def load_customers(self) -> dict[str, Customer]:
        rows = self._to_dicts(self._values("customers!A:Z"))
        return {r["customer_id"].strip(): Customer.from_row(r) for r in rows}

    def load_invoices(self) -> list[Invoice]:
        return [Invoice.from_row(r) for r in self._to_dicts(self._values("receivables!A:Z"))]

    def update_agent_fields(self, invoice_id: str, **fields) -> None:
        self._assert_writable(fields)
        values = self._values("receivables!A:Z")
        header = values[0]
        row_no = next(
            (i + 1 for i, r in enumerate(values) if r and r[0].strip() == invoice_id), None
        )
        if row_no is None:
            raise KeyError(f"账款不存在: {invoice_id}")
        data = []
        for k, v in fields.items():
            col_idx = header.index(k) if k in header else INVOICE_COLUMNS.index(k)
            col_letter = chr(ord("A") + col_idx)
            data.append({"range": f"receivables!{col_letter}{row_no}", "values": [[str(v)]]})
        self._service().values().batchUpdate(
            spreadsheetId=self.sheet_id,
            body={"valueInputOption": "RAW", "data": data},
        ).execute()
