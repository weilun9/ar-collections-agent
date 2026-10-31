"""Storage abstraction.

Why abstract it: in the product spec, Google Sheets stands in for the CRM / finance system.
In production it would be an ERP or database; as long as it implements this interface, the tiering engine and policy layer do not change.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Customer, Invoice


class PermissionDeniedError(PermissionError):
    """Raised when the agent tries to write a field it has no permission to change (L3 agent IAM)."""


class ReceivablesStore(ABC):
    #: L3 IAM: the only fields the agent's service account may write.
    #: Amount, due date, customer contact details and so on are all excluded; any attempt to write them is refused and logged.
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
                f"agent may not write fields {sorted(illegal)}; "
                f"writable fields are limited to {sorted(self.AGENT_WRITABLE_FIELDS)}"
            )
