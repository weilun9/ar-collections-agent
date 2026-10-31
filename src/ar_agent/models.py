"""Domain model."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any


class Tier(str, Enum):
    """Risk tier (product spec §4)."""
    NONE = "none"     # not yet due / settled; not in the collections flow
    LOW = "low"       # 🟢 light: handled by the agent
    MID = "mid"       # 🟡 medium: account manager notified first; agent steps in after a 48h buffer
    HIGH = "high"     # 🔴 high risk: people only; the agent sends nothing to the customer

    @property
    def label(self) -> str:
        return {"none": "—", "low": "🟢 Light", "mid": "🟡 Medium", "high": "🔴 High risk"}[self.value]

    @property
    def rank(self) -> int:
        return {"none": 0, "low": 1, "mid": 2, "high": 3}[self.value]


class AgentState(str, Enum):
    """Where an invoice sits in the agent's state machine."""
    NEW = "new"                    # not handled yet
    AUTO = "auto"                  # light tier, agent following up automatically
    AWAITING_AM = "awaiting_am"    # medium tier, account manager notified, 48h buffer running
    HUMAN_TAKEN = "human_taken"    # account manager / finance has taken over; the agent stops all actions
    ESCALATED = "escalated"        # escalated to a person (high risk / forced by keywords)
    PAUSED = "paused"              # paused by a circuit breaker or policy
    CLOSED = "closed"              # settled


class ActionType(str, Enum):
    WHATSAPP_REMINDER = "whatsapp_reminder"
    GMAIL_DUNNING = "gmail_dunning"
    SLACK_AM_NOTICE = "slack_am_notice"
    SLACK_FINANCE_ALERT = "slack_finance_alert"
    GMAIL_FINANCE_ALERT = "gmail_finance_alert"
    ESCALATE = "escalate"
    HOLD = "hold"                  # no action this run (buffer not over / taken over, etc.)
    SKIP = "skip"
    WEEKLY_REPORT = "weekly_report"


class Actor(str, Enum):
    """Core L5 observability field: who actually performed this action."""
    AGENT = "agent"
    HUMAN = "human"
    SYSTEM = "system"


@dataclass
class Customer:
    customer_id: str
    name: str
    contact_name: str
    whatsapp: str
    email: str
    account_manager: str
    am_slack_id: str
    is_vip: bool
    prior_late_count: int
    avg_days_to_pay: int
    credit_terms_days: int

    @classmethod
    def from_row(cls, row: dict[str, str]) -> "Customer":
        return cls(
            customer_id=row["customer_id"].strip(),
            name=row["name"].strip(),
            contact_name=row["contact_name"].strip(),
            whatsapp=row["whatsapp"].strip(),
            email=row["email"].strip(),
            account_manager=row["account_manager"].strip(),
            am_slack_id=row["am_slack_id"].strip(),
            is_vip=row["is_vip"].strip().lower() in ("1", "true", "yes", "y"),
            prior_late_count=int(row["prior_late_count"] or 0),
            avg_days_to_pay=int(row["avg_days_to_pay"] or 0),
            credit_terms_days=int(row["credit_terms_days"] or 30),
        )


@dataclass
class Invoice:
    # ---- Business master data: read-only for the agent, no write permission (L3 IAM) ----
    invoice_id: str
    customer_id: str
    amount: float
    issue_date: date
    due_date: date
    status: str              # open / paid / disputed
    paid_date: date | None

    # ---- Agent state fields: the only columns the agent may write (L3 IAM) ----
    agent_tier: str = ""
    agent_stage: int = 0             # number of automatic reminders sent
    agent_state: str = AgentState.NEW.value
    agent_last_action_at: str = ""
    agent_buffer_until: str = ""     # end of the 48h buffer for medium-tier invoices
    agent_owner: str = ""            # who took over (account manager / finance)
    agent_note: str = ""

    def days_overdue(self, today: date) -> int:
        return (today - self.due_date).days

    def is_open(self) -> bool:
        return self.status.strip().lower() == "open"

    @classmethod
    def from_row(cls, row: dict[str, str]) -> "Invoice":
        def d(v: str) -> date | None:
            v = (v or "").strip()
            return datetime.strptime(v, "%Y-%m-%d").date() if v else None

        return cls(
            invoice_id=row["invoice_id"].strip(),
            customer_id=row["customer_id"].strip(),
            amount=float(row["amount"] or 0),
            issue_date=d(row["issue_date"]),
            due_date=d(row["due_date"]),
            status=row["status"].strip(),
            paid_date=d(row.get("paid_date", "")),
            agent_tier=row.get("agent_tier", "").strip(),
            agent_stage=int(row.get("agent_stage") or 0),
            agent_state=row.get("agent_state", "").strip() or AgentState.NEW.value,
            agent_last_action_at=row.get("agent_last_action_at", "").strip(),
            agent_buffer_until=row.get("agent_buffer_until", "").strip(),
            agent_owner=row.get("agent_owner", "").strip(),
            agent_note=row.get("agent_note", "").strip(),
        )


@dataclass
class Decision:
    """Final verdict for one invoice after tiering and the policy layer."""
    invoice: Invoice
    customer: Customer
    tier: Tier
    base_tier: Tier                       # tier from the tiering engine alone, before the policy layer
    action: ActionType
    reasons: list[str] = field(default_factory=list)
    policy_hits: list[str] = field(default_factory=list)
    agent_may_contact_customer: bool = True
    detail: dict[str, Any] = field(default_factory=dict)

    def add_reason(self, text: str) -> None:
        self.reasons.append(text)


@dataclass
class Message:
    """An outgoing message. Note: body is rendered from a template; callers cannot pass free text."""
    channel: str
    to: str
    subject: str
    body: str
    template_id: str
    invoice_id: str
    customer_id: str
    audience: str    # "customer" (external) / "internal"
    # Structured variables used to render body. WhatsApp template messages are sent using the template approved
    # on Meta's side, so they need the variables themselves rather than the rendered text.
    variables: dict = field(default_factory=dict)


@dataclass
class SendResult:
    ok: bool
    channel: str
    to: str
    mode: str          # live / dry_run
    detail: str = ""
    ref: str = ""
