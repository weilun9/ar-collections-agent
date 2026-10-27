"""分级引擎 —— PM 文档 §4 决策矩阵。

设计原则：**由高到低短路判定**。先看是不是高风险，再看是不是中度，都不是才落轻度。
这样保证"漏判方向"永远是保守的：一条账款只可能被判得更严，不会被判得更松。
（对应 §八 Q1 的非对称风险控制：宁可误报增加人工审查，不可漏报导致自动化误伤客户。）

注意：这一层只做"客观事实 → 分级"的纯函数映射，不做任何动作决策，
也不涉及 VIP / 关键词 / 熔断这些运行时策略 —— 那些属于 policy.py（L4）。
分开的好处是分级逻辑可以单独单元测试，且策略层永远跑在分级层之后、可以覆盖它。
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
        return TierResult(Tier.NONE, [f"账款状态为 {invoice.status}，不进入催收流"], days)

    if days < t["low"]["overdue_days_min"]:
        return TierResult(Tier.NONE, [f"尚未逾期（距到期 {-days} 天）"], days)

    # ---- 🔴 高风险：任一条件命中即成立 ----
    high = t["high"]
    if days >= high["overdue_days_min"]:
        reasons.append(f"逾期 {days} 天 ≥ {high['overdue_days_min']} 天")
    if invoice.amount > high["amount_min"]:
        reasons.append(f"金额 ¥{invoice.amount:,.0f} > ¥{high['amount_min']:,.0f}")
    if reasons:
        return TierResult(Tier.HIGH, reasons, days)

    # ---- 🟡 中度：任一条件命中即成立 ----
    mid = t["mid"]
    if days >= mid["overdue_days_min"]:
        reasons.append(f"逾期 {days} 天 ≥ {mid['overdue_days_min']} 天")
    if invoice.amount >= mid["amount_min"]:
        reasons.append(f"金额 ¥{invoice.amount:,.0f} ≥ ¥{mid['amount_min']:,.0f}")
    if customer.prior_late_count >= mid["prior_late_count_min"]:
        reasons.append(f"客户历史逾期 {customer.prior_late_count} 次")
    if reasons:
        return TierResult(Tier.MID, reasons, days)

    # ---- 🟢 轻度：走到这里说明高/中条件全不命中 ----
    return TierResult(
        Tier.LOW,
        [f"逾期 {days} 天、金额 ¥{invoice.amount:,.0f}、客户历史无逾期"],
        days,
    )
