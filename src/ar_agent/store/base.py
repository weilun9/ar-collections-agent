"""存储抽象层。

之所以抽象：PM 文档里 Google Sheets 是"代替 CRM/财务系统"的占位。
真上生产时换成 ERP/数据库，只要实现同一个接口，上层分级引擎与策略层一行不用改。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Customer, Invoice


class PermissionDeniedError(PermissionError):
    """Agent 试图写入它无权修改的字段时抛出（L3 Agent IAM）。"""


class ReceivablesStore(ABC):
    #: L3 IAM —— Agent 的 Service Account 唯一可写的字段白名单。
    #: 金额 / 到期日 / 客户联系方式等全部不在内，写入尝试会被直接拒绝并留痕。
    AGENT_WRITABLE_FIELDS: frozenset[str] = frozenset({
        "agent_tier",
        "agent_stage",
        "agent_state",
        "agent_last_action_at",
        "agent_buffer_until",
        "agent_owner",
        "agent_note",
    })

    @abstractmethod
    def load_customers(self) -> dict[str, Customer]: ...

    @abstractmethod
    def load_invoices(self) -> list[Invoice]: ...

    @abstractmethod
    def update_agent_fields(self, invoice_id: str, **fields) -> None: ...

    def _assert_writable(self, fields: dict) -> None:
        illegal = set(fields) - self.AGENT_WRITABLE_FIELDS
        if illegal:
            raise PermissionDeniedError(
                f"Agent 无权写入字段 {sorted(illegal)}；"
                f"可写字段仅限 {sorted(self.AGENT_WRITABLE_FIELDS)}"
            )
