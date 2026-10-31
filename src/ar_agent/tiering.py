"""Tiering engine (product spec §4 decision matrix).

Design rule: **check from high to low and stop at the first match**. High risk first, then medium, and only then light.
This keeps every mistake on the safe side: an invoice can only be tiered more strictly, never more loosely.
(Spec Q1, asymmetric risk: a false alarm costs some human review; a missed one can harm a customer relationship.)

Note: this layer is a pure mapping from objective facts to a tier. It makes no action decisions
and knows nothing about runtime policy such as VIPs, keywords or circuit breakers; those live in policy.py (L4).
Keeping them apart means tiering can be unit-tested alone, and the policy layer always runs after it and can override it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .config import Config
from .models import Customer, Invoice, Tier


@dataclass
class TierResult:
    tier: Tier
    reasons: list[str]
    days_overdue: int


def classify(invoice: Invoice, customer: Customer, cfg: Config, today: date) -> TierResult:
    days = invoice.days_overdue(today)
    t = cfg.tiering
    reasons: list[str] = []

    if not invoice.is_open():
        return TierResult(Tier.NONE, [f"invoice status is {invoice.status}; not in the collections flow"], days)

    if days < t["low"]["overdue_days_min"]:
        return TierResult(Tier.NONE, [f"not yet overdue (due in {-days} days)"], days)

    # ---- 🔴 High risk: any one condition is enough ----
    high = t["high"]
    if days >= high["overdue_days_min"]:
        reasons.append(f"{days} days overdue ≥ {high['overdue_days_min']} days")
    if invoice.amount > high["amount_min"]:
        reasons.append(f"amount ¥{invoice.amount:,.0f} > ¥{high['amount_min']:,.0f}")
    if reasons:
        return TierResult(Tier.HIGH, reasons, days)

    # ---- 🟡 Medium: any one condition is enough ----
    mid = t["mid"]
    if days >= mid["overdue_days_min"]:
        reasons.append(f"{days} days overdue ≥ {mid['overdue_days_min']} days")
    if invoice.amount >= mid["amount_min"]:
        reasons.append(f"amount ¥{invoice.amount:,.0f} ≥ ¥{mid['amount_min']:,.0f}")
    if customer.prior_late_count >= mid["prior_late_count_min"]:
        reasons.append(f"customer has paid late {customer.prior_late_count} times before")
    if reasons:
        return TierResult(Tier.MID, reasons, days)

    # ---- 🟢 Light: reaching here means no high or medium condition matched ----
    return TierResult(
        Tier.LOW,
        [f"{days} days overdue, amount ¥{invoice.amount:,.0f}, no late-payment history"],
        days,
    )
