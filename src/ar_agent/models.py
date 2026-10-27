"""领域模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any


class Tier(str, Enum):
    """风险分级（PM 文档 §4）。"""
    NONE = "none"     # 未到期 / 已结清，不进入催收流
    LOW = "low"       # 🟢 轻度 —— Agent 自主处理
    MID = "mid"       # 🟡 中度 —— 先通知客户经理，48h 缓冲后 Agent 补位
    HIGH = "high"     # 🔴 高风险 —— 纯人工，Agent 不对外发送任何消息

    @property
    def label(self) -> str:
        return {"none": "—", "low": "🟢 轻度", "mid": "🟡 中度", "high": "🔴 高风险"}[self.value]

    @property
    def rank(self) -> int:
        return {"none": 0, "low": 1, "mid": 2, "high": 3}[self.value]


class AgentState(str, Enum):
    """账款在 Agent 流程里的状态机位置。"""
    NEW = "new"                    # 尚未被处理过
    AUTO = "auto"                  # 轻度，Agent 自动跟进中
    AWAITING_AM = "awaiting_am"    # 中度，已通知客户经理，48h 缓冲期计时中
    HUMAN_TAKEN = "human_taken"    # 客户经理/财务已接管，Agent 停止一切动作
    ESCALATED = "escalated"        # 已升级人工（高风险 / 关键词强制升级）
    PAUSED = "paused"              # 被熔断或策略暂停
    CLOSED = "closed"              # 已结清


class ActionType(str, Enum):
    WHATSAPP_REMINDER = "whatsapp_reminder"
    GMAIL_DUNNING = "gmail_dunning"
    SLACK_AM_NOTICE = "slack_am_notice"
    SLACK_FINANCE_ALERT = "slack_finance_alert"
    GMAIL_FINANCE_ALERT = "gmail_finance_alert"
    ESCALATE = "escalate"
    HOLD = "hold"                  # 本轮不动作（缓冲期未到 / 已接管等）
    SKIP = "skip"
    WEEKLY_REPORT = "weekly_report"


class Actor(str, Enum):
    """L5 可观测性核心字段：这条动作到底是谁做的。"""
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
    # ---- 业务主数据：Agent 只读，无写权限（L3 IAM）----
    invoice_id: str
    customer_id: str
    amount: float
    issue_date: date
    due_date: date
    status: str              # open / paid / disputed
    paid_date: date | None

    # ---- Agent 状态字段：Agent 唯一有写权限的列（L3 IAM）----
    agent_tier: str = ""
    agent_stage: int = 0             # 已发出的自动提醒次数
    agent_state: str = AgentState.NEW.value
    agent_last_action_at: str = ""
    agent_buffer_until: str = ""     # 中度分级的 48h 缓冲期截止时间
    agent_owner: str = ""            # 接管人（客户经理 / 财务）
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
    """一条账款经过分级 + 策略层后的最终裁决。"""
    invoice: Invoice
    customer: Customer
    tier: Tier
    base_tier: Tier                       # 策略层介入前，纯分级引擎给出的等级
    action: ActionType
    reasons: list[str] = field(default_factory=list)
    policy_hits: list[str] = field(default_factory=list)
    agent_may_contact_customer: bool = True
    detail: dict[str, Any] = field(default_factory=dict)

    def add_reason(self, text: str) -> None:
        self.reasons.append(text)


@dataclass
class Message:
    """待发送消息。注意：body 由模板渲染产生，调用方无法自由传入正文。"""
    channel: str
    to: str
    subject: str
    body: str
    template_id: str
    invoice_id: str
    customer_id: str
    audience: str    # "customer" 对外 / "internal" 对内
    # 渲染 body 时用到的结构化变量。WhatsApp 模板消息按 Meta 审批过的模板发送，
    # 需要的是变量本身而不是渲染后的正文。
    variables: dict = field(default_factory=dict)


@dataclass
class SendResult:
    ok: bool
    channel: str
    to: str
    mode: str          # live / dry_run
    detail: str = ""
    ref: str = ""
