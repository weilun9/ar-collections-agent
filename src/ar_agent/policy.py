"""L4 runtime policy layer: the core line of defence in product spec §5.

Design points:

    **It runs outside the LLM, and after the tiering engine.**

Writing "any change to payment terms must go to a person" in a system prompt is a **soft constraint**: the model may ignore it,
prompt injection may bypass it, and it may be forgotten in a long context. This layer is Python code:
it reads no prompt and calls no model; it only checks facts against thresholds and acts on a match. The agent has no way to talk it round.

Order: the tiering engine gives base_tier -> each rule in the chain runs -> final tier and whether customer contact is allowed.
Each rule can only make the result **stricter** (escalate, forbid customer contact), never looser. This monotonicity is deliberate:
adding a rule can never accidentally open a door that was closed.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date

from .config import Config
from .models import Customer, Invoice, Tier


@dataclass
class PolicyContext:
    """Everything a policy decision needs."""
    invoice: Invoice
    customer: Customer
    base_tier: Tier
    today: date
    sends_today: int = 0
    inbound_intent: str | None = None      # from inbound.classify_reply
    inbound_summary: str = ""


@dataclass
class PolicyOutcome:
    tier: Tier
    agent_may_contact_customer: bool = True
    hits: list[str] = field(default_factory=list)
    halt_all_sending: bool = False         # global pause (daily send cap)

    def escalate_to(self, tier: Tier, reason: str) -> None:
        """Escalate only, never downgrade: the policy layer never makes an invoice's tier lighter."""
        if tier.rank > self.tier.rank:
            self.tier = tier
        self.hits.append(reason)

    def forbid_customer_contact(self, reason: str) -> None:
        self.agent_may_contact_customer = False
        if reason not in self.hits:
            self.hits.append(reason)


class PolicyRule(ABC):
    id: str = ""
    description: str = ""

    @abstractmethod
    def apply(self, ctx: PolicyContext, out: PolicyOutcome, cfg: Config) -> None: ...


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #

class TermsChangeForcesHigh(PolicyRule):
    """The customer asks to change payment terms -> unconditionally escalate to high risk, whatever the current tier.

    Product spec §4.2. This is "complex logic that cannot be bypassed" in practice:
    the trigger comes from plain keyword matching in inbound.py, not from a model's reading of the message.
    """
    id = "P-01"
    description = "Request to change payment terms (instalments / discount / extension) forces high risk"

    def apply(self, ctx, out, cfg):
        if not cfg.policy.get("terms_change_forces_high", True):
            return
        if ctx.inbound_intent == "terms_change":
            out.escalate_to(Tier.HIGH, f"{self.id} request to change payment terms -> forced high risk ({ctx.inbound_summary})")
            out.forbid_customer_contact(f"{self.id} changing terms needs a human decision; the agent stops all customer contact")


class DisputeForcesHigh(PolicyRule):
    """Billing dispute / legal signal -> forced to high risk."""
    id = "P-02"
    description = "Billing dispute or legal signal forces high risk"

    def apply(self, ctx, out, cfg):
        if not cfg.policy.get("dispute_forces_high", True):
            return
        if ctx.inbound_intent == "dispute":
            out.escalate_to(Tier.HIGH, f"{self.id} billing dispute / legal signal -> forced high risk ({ctx.inbound_summary})")
            out.forbid_customer_contact(f"{self.id} the agent may not keep chasing until the dispute is resolved")


class HardAmountCeiling(PolicyRule):
    """Invoices above the amount ceiling can never be handled automatically, whatever their tier.

    Product spec §5 L4: "any invoice above ¥200,000 must never be handled automatically by the agent, whatever its tier".
    Note this is a **separate rule** from the high-risk amount threshold in the tiering engine; the two just share a value today.
    It is not redundant: the tiering threshold is a risk label for people, this one is an action gate for the machine.
    If the business later wants the high-risk label at 150,000 but the automation ceiling to stay at 200,000, it is a config change,
    not a code change: labels and permissions should be decoupled.
    """
    id = "P-03"
    description = "Invoices above the amount ceiling may not be handled automatically by the agent"

    def apply(self, ctx, out, cfg):
        ceiling = cfg.policy["hard_human_only_amount"]
        if ctx.invoice.amount > ceiling:
            out.escalate_to(Tier.HIGH, f"{self.id} amount ¥{ctx.invoice.amount:,.0f} > ceiling ¥{ceiling:,.0f}")
            out.forbid_customer_contact(f"{self.id} invoices above the ceiling are handled by people only")


class VipAlwaysHuman(PolicyRule):
    """VIP customers always go to a person.

    Product spec §6.2 guardrail metric: "number of times a VIP customer was chased automatically by the agent: target 0".
    A target of 0 cannot rest on hoping it never happens; a rule has to make it impossible at runtime.
    That is how guardrail metrics map to the policy layer: every guardrail metric should have a rule behind it.
    """
    id = "P-04"
    description = "VIP customers are never chased automatically; always handled by a person"

    def apply(self, ctx, out, cfg):
        if cfg.policy.get("vip_always_human", True) and ctx.customer.is_vip:
            out.escalate_to(Tier.HIGH, f"{self.id} VIP customer {ctx.customer.name} -> forced to a person")
            out.forbid_customer_contact(f"{self.id} VIP customers do not receive automated reminders")


class DailySendCap(PolicyRule):
    """Daily send volume over the cap -> pause and notify finance.

    A backstop against a logic bug that blasts every customer. Even if the tiering engine is completely wrong,
    at most N customers can be contacted in a day, so the damage is bounded.
    """
    id = "P-05"
    description = "Pause all sending when the daily customer send volume reaches the cap"

    def apply(self, ctx, out, cfg):
        cap = cfg.policy["daily_send_cap"]
        if ctx.sends_today >= cap:
            out.halt_all_sending = True
            out.forbid_customer_contact(f"{self.id} daily send cap of {cap} reached; pausing and notifying finance")


class PaidClaimPauses(PolicyRule):
    """The customer says it has paid -> pause collection and let finance check the payment.

    The spec does not state this rule explicitly, but it is necessary: chasing a customer who has already paid, before finance confirms receipt,
    is the most damaging kind of mistake and is entirely avoidable. Same approach as a dispute: stop automatic actions and hand over to a person.
    """
    id = "P-06"
    description = "Pause automated collection when the customer says it has paid, until finance checks"

    def apply(self, ctx, out, cfg):
        if ctx.inbound_intent == "paid_claim":
            out.forbid_customer_contact(f"{self.id} customer says it has paid; collection stops until finance checks")


#: The rule chain. Order does not matter (every rule only tightens), but it is kept in ID order for readable logs and audits.
DEFAULT_RULES: list[PolicyRule] = [
    TermsChangeForcesHigh(),
    DisputeForcesHigh(),
    HardAmountCeiling(),
    VipAlwaysHuman(),
    DailySendCap(),
    PaidClaimPauses(),
]


def evaluate(ctx: PolicyContext, cfg: Config, rules: list[PolicyRule] | None = None) -> PolicyOutcome:
    """Run the whole rule chain and return the final verdict."""
    out = PolicyOutcome(tier=ctx.base_tier)
    for rule in (rules if rules is not None else DEFAULT_RULES):
        rule.apply(ctx, out, cfg)
    return out


def rule_catalog() -> list[dict[str, str]]:
    """Rule list for the dashboard and audit reports."""
    return [{"id": r.id, "description": r.description} for r in DEFAULT_RULES]
