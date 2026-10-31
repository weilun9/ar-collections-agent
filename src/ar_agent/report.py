"""Weekly report (product spec §3.1 + §6 success metrics).

The CFO's "overdue health report I can read any time without chasing finance".

The report has three parts on purpose, matching the metric design in §6:
    1. Cash-flow health   -- the North Star and the ageing profile
    2. Agent operations   -- automation share, response time, coverage
    3. Guardrail metrics  -- counter-metrics that answer "did efficiency cost us customer relationships?"

Part 3 sits in the report rather than a monitoring back end on purpose:
if only the people building the system can see the guardrail metrics, they never really constrain decisions.
The figure "VIP customers chased automatically: 0" only carries weight when the CFO sees it every week.
"""
from __future__ import annotations

import html
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from .audit import AuditLog
from .config import Config
from .models import Customer, Invoice, Tier
from .store import CsvStore

AGING_BUCKETS = [(1, 14, "1-14 days"), (15, 30, "15-30 days"),
                 (31, 60, "31-60 days"), (61, 10**6, "over 60 days")]


@dataclass
class WeeklyMetrics:
    as_of: date
    total_open_amount: float = 0.0
    overdue_amount: float = 0.0
    overdue_count: int = 0
    open_count: int = 0
    dso_proxy: float = 0.0
    aging: dict[str, dict] = field(default_factory=dict)
    tier_amount: dict[str, float] = field(default_factory=dict)
    tier_count: dict[str, int] = field(default_factory=dict)
    agent_actions: int = 0
    human_actions: int = 0
    coverage_rate: float = 0.0
    first_touch_hours: float | None = None
    first_touch_sample: int = 0
    first_touch_backlog_excluded: int = 0
    am_unresponsive: list[dict] = field(default_factory=list)
    vip_auto_contacted: int = 0
    wrong_escalations: int = 0
    escalated: list[dict] = field(default_factory=list)
    top_exposure: list[dict] = field(default_factory=list)


def compute(root: Path, cfg: Config, as_of: date | None = None) -> WeeklyMetrics:
    as_of = as_of or date.today()
    store = CsvStore(root / "data")
    audit = AuditLog(root / "data" / "action_log.csv")
    customers = store.load_customers()
    invoices = store.load_invoices()
    log = audit.read_all()

    m = WeeklyMetrics(as_of=as_of)

    open_invoices = [i for i in invoices if i.is_open()]
    m.open_count = len(open_invoices)
    m.total_open_amount = sum(i.amount for i in open_invoices)

    overdue = [i for i in open_invoices if i.days_overdue(as_of) >= 1]
    m.overdue_count = len(overdue)
    m.overdue_amount = sum(i.amount for i in overdue)

    # DSO approximation: amount-weighted average days outstanding.
    # True DSO needs credit sales for the period, which live in the ERP, not in this receivables table.
    # The weighted age is used as a proxy and labelled as such in the report: better clearly labelled than precise-looking and wrong.
    if m.total_open_amount:
        weighted = sum(i.amount * max((as_of - i.issue_date).days, 0) for i in open_invoices)
        m.dso_proxy = weighted / m.total_open_amount

    for lo, hi, label in AGING_BUCKETS:
        rows = [i for i in overdue if lo <= i.days_overdue(as_of) <= hi]
        m.aging[label] = {"count": len(rows), "amount": sum(i.amount for i in rows)}

    for i in overdue:
        t = i.agent_tier or "unclassified"
        m.tier_amount[t] = m.tier_amount.get(t, 0.0) + i.amount
        m.tier_count[t] = m.tier_count.get(t, 0) + 1

    # ---- Agent vs people ----
    real_actions = [r for r in log if r.get("action") not in ("hold", "skip", "batch_capped", "")]
    m.agent_actions = sum(1 for r in real_actions if r.get("actor") == "agent")
    m.human_actions = sum(1 for r in real_actions if r.get("actor") == "human")

    # ---- Coverage: has every overdue invoice been handled by the system (target 100%, none missed) ----
    touched = {r["invoice_id"] for r in log if r.get("invoice_id")}
    if overdue:
        m.coverage_rate = len([i for i in overdue if i.invoice_id in touched]) / len(overdue) * 100

    # ---- Time to first touch: how long after going overdue the first action is sent ----
    #
    # Definition:
    # only invoices that became overdue **after the system went live** are counted. An invoice already 60 days overdue on day one
    # would show "first touch = 1440 hours", which measures old debt from before go-live, not the agent's response time.
    # Including that backlog gives a large, meaningless number; excluding it,
    # the metric really answers "from now on, how long after going overdue is an invoice followed up?"
    inv_map = {i.invoice_id: i for i in invoices}
    go_live: datetime | None = None
    for r in log:
        ts = r.get("timestamp", "")
        if ts:
            try:
                go_live = datetime.fromisoformat(ts)
                break
            except ValueError:
                continue

    first_by_inv: dict[str, str] = {}
    for r in log:
        inv_id = r.get("invoice_id")
        if inv_id and r.get("actor") == "agent" and r.get("action") not in ("hold", "skip"):
            first_by_inv.setdefault(inv_id, r.get("timestamp", ""))

    gaps: list[float] = []
    backlog = 0
    for inv_id, ts in first_by_inv.items():
        inv = inv_map.get(inv_id)
        if not inv or not ts or not inv.due_date:
            continue
        try:
            acted = datetime.fromisoformat(ts)
        except ValueError:
            continue
        # The overdue clock starts at **midnight after** the due date: paying on the due date is still on time
        # (consistent with the days_overdue >= 1 rule).
        # Starting from midnight of the due date would add 24 hours to every invoice and make the "< 24 hours" target meaningless.
        overdue_since = datetime.combine(inv.due_date + timedelta(days=1), datetime.min.time())
        if go_live is not None and overdue_since < go_live.replace(
                hour=0, minute=0, second=0, microsecond=0):
            backlog += 1                      # already overdue before go-live -> backlog, not counted
            continue
        hours = (acted - overdue_since).total_seconds() / 3600
        if hours >= 0:
            gaps.append(hours)
    m.first_touch_backlog_excluded = backlog
    if gaps:
        m.first_touch_hours = sum(gaps) / len(gaps)
        m.first_touch_sample = len(gaps)

    # ---- Account manager did not respond (spec Q2: a management signal in itself) ----
    unresponsive: dict[str, list] = defaultdict(list)
    for r in log:
        if "did not respond" in (r.get("reasons") or "") or "未响应" in (r.get("reasons") or ""):  # second check matches logs from older versions
            inv = inv_map.get(r.get("invoice_id", ""))
            if inv:
                cust = customers.get(inv.customer_id)
                if cust:
                    unresponsive[cust.account_manager].append(inv.invoice_id)
    m.am_unresponsive = [
        {"account_manager": am, "count": len(v), "invoices": v}
        for am, v in sorted(unresponsive.items(), key=lambda kv: -len(kv[1]))
    ]

    # ---- Guardrail metrics ----
    vip_ids = {c.customer_id for c in customers.values() if c.is_vip}
    m.vip_auto_contacted = sum(
        1 for r in log
        if r.get("actor") == "agent"
        and r.get("customer_id") in vip_ids
        and r.get("channel") in ("whatsapp", "gmail")      # customer-facing channels only
        and r.get("ok") == "1"
    )
    # Wrong-escalation count: invoices that should have gone to a person but were contacted automatically by the agent
    m.wrong_escalations = sum(
        1 for r in log
        if r.get("actor") == "agent"
        and r.get("tier") == Tier.HIGH.value
        and r.get("channel") in ("whatsapp", "gmail")
        and r.get("ok") == "1"
    )

    m.escalated = [
        {
            "invoice_id": i.invoice_id,
            "customer": customers[i.customer_id].name if i.customer_id in customers else i.customer_id,
            "amount": i.amount,
            "days": i.days_overdue(as_of),
            "note": i.agent_note,
        }
        for i in overdue if i.agent_tier == Tier.HIGH.value
    ]
    m.escalated.sort(key=lambda x: -x["amount"])

    exposure: Counter = Counter()
    for i in overdue:
        exposure[i.customer_id] += i.amount
    m.top_exposure = [
        {"customer": customers[cid].name if cid in customers else cid,
         "amount": amt,
         "vip": cid in vip_ids}
        for cid, amt in exposure.most_common(5)
    ]
    return m


def _money(v: float) -> str:
    return f"¥{v:,.0f}"


def render_markdown(m: WeeklyMetrics, cfg: Config) -> str:
    period_start = m.as_of - timedelta(days=7)
    L: list[str] = []
    add = L.append

    add(f"# Accounts Receivable Health · Weekly Report")
    add(f"\n**{cfg.company['name']}** · period {period_start} to {m.as_of}\n")
    add("> Generated automatically by the AR collections agent. The full record of every customer-facing action is in `data/action_log.csv`.\n")

    add("\n## 1. Cash-flow health\n")
    add("| Metric | Value |")
    add("|---|---|")
    add(f"| Total open receivables | {_money(m.total_open_amount)} ({m.open_count} invoices) |")
    add(f"| Of which overdue | **{_money(m.overdue_amount)}** ({m.overdue_count} invoices) |")
    pct = (m.overdue_amount / m.total_open_amount * 100) if m.total_open_amount else 0
    add(f"| Overdue share | {pct:.1f}% |")
    add(f"| Weighted average days outstanding (DSO proxy) | {m.dso_proxy:.1f} days |")
    add("\n> DSO proxy = Σ(amount × days outstanding) / Σ(amount). "
        "True DSO needs credit sales for the period, which sit in the ERP; once connected, the standard definition can replace it.\n")

    add("\n### Ageing\n")
    add("| Age | Invoices | Amount | Share of overdue |")
    add("|---|---:|---:|---:|")
    for _, _, label in AGING_BUCKETS:
        b = m.aging.get(label, {"count": 0, "amount": 0})
        share = (b["amount"] / m.overdue_amount * 100) if m.overdue_amount else 0
        add(f"| {label} | {b['count']} | {_money(b['amount'])} | {share:.1f}% |")

    add("\n### Risk tiers\n")
    add("| Tier | Invoices | Amount |")
    add("|---|---:|---:|")
    for tier, label in [("low", "🟢 Light"), ("mid", "🟡 Medium"), ("high", "🔴 High risk")]:
        add(f"| {label} | {m.tier_count.get(tier, 0)} | {_money(m.tier_amount.get(tier, 0))} |")

    add("\n## 2. Agent operations\n")
    total = m.agent_actions + m.human_actions
    auto_pct = (m.agent_actions / total * 100) if total else 0
    add("| Metric | Value | Target |")
    add("|---|---|---|")
    add(f"| Actions completed by the agent | {m.agent_actions} ({auto_pct:.0f}%) | 80% autonomous on the light tier |")
    add(f"| Actions by people | {m.human_actions} | — |")
    add(f"| Overdue invoice coverage | **{m.coverage_rate:.0f}%** | 100% (none missed) |")
    if m.first_touch_hours is not None:
        add(f"| First touch after going overdue | **{m.first_touch_hours:.1f} hours**"
            f" ({m.first_touch_sample} invoices sampled) | < 24 hours (previously 5-7 days) |")
    else:
        add("| First touch after going overdue | not enough data | < 24 hours (previously 5-7 days) |")
    if m.first_touch_backlog_excluded:
        add("\n> Time to first touch only counts invoices that became overdue after go-live; "
            f"the {m.first_touch_backlog_excluded} invoices already overdue at go-live are excluded, "
            "because their age reflects old debt from before go-live, not the agent's response time.\n")

    if m.am_unresponsive:
        add("\n### Account manager response within the buffer\n")
        add("> Medium-risk invoices whose account manager did not take over within the 48-hour buffer. "
            "This is a management signal in itself, showing how promptly people respond.\n")
        add("| Account manager | Not responded | Invoices |")
        add("|---|---:|---|")
        for r in m.am_unresponsive:
            add(f"| {r['account_manager']} | {r['count']} | {', '.join(r['invoices'])} |")

    add("\n## 3. Guardrail metrics (counter-metrics)\n")
    add("> This part answers one question: did we trade customer relationships for efficiency?\n")
    add("| Metric | This period | Target | Status |")
    add("|---|---:|---:|---|")
    ok = lambda v: "✅ on target" if v == 0 else "⚠️ needs review"       # noqa: E731
    add(f"| VIP customers chased automatically by the agent | {m.vip_auto_contacted} | 0 | {ok(m.vip_auto_contacted)} |")
    add(f"| Wrong escalations (should have gone to a person, contacted automatically) | {m.wrong_escalations} | 0 | {ok(m.wrong_escalations)} |")

    if m.escalated:
        add("\n## 4. High-risk invoices awaiting a human decision\n")
        add("> The system has **sent no automated message** to these customers; a person must decide the next step.\n")
        add("| Invoice | Customer | Amount | Overdue | Escalation reason |")
        add("|---|---|---:|---:|---|")
        for e in m.escalated[:15]:
            add(f"| {e['invoice_id']} | {e['customer']} | {_money(e['amount'])} | "
                f"{e['days']} days | {e['note'][:60]} |")

    if m.top_exposure:
        add("\n## 5. Top 5 customers by overdue exposure\n")
        add("| Customer | Overdue amount |")
        add("|---|---:|")
        for e in m.top_exposure:
            tag = " ⭐VIP" if e["vip"] else ""
            add(f"| {e['customer']}{tag} | {_money(e['amount'])} |")

    add(f"\n---\n*Generated: {datetime.now():%Y-%m-%d %H:%M}*")
    return "\n".join(L)


def render_html(markdown_text: str, m: WeeklyMetrics, cfg: Config) -> str:
    """Minimal Markdown -> HTML (only the syntax this report uses; no third-party library)."""
    out: list[str] = []
    in_table = False
    for line in markdown_text.split("\n"):
        s = line.rstrip()
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if all(set(c) <= set("-: ") and c for c in cells):
                continue
            if not in_table:
                out.append("<table>")
                in_table = True
                tag = "th"
            else:
                tag = "td"
            row = "".join(f"<{tag}>{_inline(c)}</{tag}>" for c in cells)
            out.append(f"<tr>{row}</tr>")
            continue
        if in_table:
            out.append("</table>")
            in_table = False
        if s.startswith("### "):
            out.append(f"<h3>{_inline(s[4:])}</h3>")
        elif s.startswith("## "):
            out.append(f"<h2>{_inline(s[3:])}</h2>")
        elif s.startswith("# "):
            out.append(f"<h1>{_inline(s[2:])}</h1>")
        elif s.startswith("> "):
            out.append(f"<blockquote>{_inline(s[2:])}</blockquote>")
        elif s.startswith("---"):
            out.append("<hr>")
        elif s:
            out.append(f"<p>{_inline(s)}</p>")
    if in_table:
        out.append("</table>")

    body = "\n".join(out)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Accounts Receivable Weekly Report · {m.as_of}</title>
<style>
 body{{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;max-width:900px;
      margin:0 auto;padding:40px 24px;color:#1a1a1a;line-height:1.7;background:#fff}}
 h1{{font-size:26px;border-bottom:3px solid #2563eb;padding-bottom:10px;margin-bottom:6px}}
 h2{{font-size:19px;margin-top:36px;color:#1e3a8a}}
 h3{{font-size:15px;margin-top:24px;color:#334155}}
 table{{border-collapse:collapse;width:100%;margin:14px 0;font-size:14px}}
 th,td{{border:1px solid #e2e8f0;padding:8px 12px;text-align:left}}
 th{{background:#f1f5f9;font-weight:600}}
 tr:nth-child(even) td{{background:#fafbfc}}
 blockquote{{border-left:3px solid #cbd5e1;margin:12px 0;padding:6px 16px;
             color:#475569;background:#f8fafc;font-size:13px}}
 hr{{border:0;border-top:1px solid #e2e8f0;margin:32px 0}}
 code{{background:#f1f5f9;padding:2px 6px;border-radius:4px;font-size:13px}}
 strong{{color:#0f172a}}
</style></head><body>
{body}
</body></html>"""


def _inline(text: str) -> str:
    t = html.escape(text)
    t = t.replace("**", "\x00")
    parts = t.split("\x00")
    if len(parts) > 1:
        t = "".join(p if n % 2 == 0 else f"<strong>{p}</strong>" for n, p in enumerate(parts))
    while "`" in t:
        a = t.find("`")
        b = t.find("`", a + 1)
        if b == -1:
            break
        t = t[:a] + "<code>" + t[a + 1:b] + "</code>" + t[b + 1:]
    return t


def generate(root: Path, cfg: Config, as_of: date | None = None) -> tuple[Path, Path, WeeklyMetrics]:
    m = compute(root, cfg, as_of)
    md = render_markdown(m, cfg)
    html_text = render_html(md, m, cfg)

    reports_dir = root / "reports"
    reports_dir.mkdir(exist_ok=True)
    stamp = (as_of or date.today()).isoformat()
    md_path = reports_dir / f"weekly-{stamp}.md"
    html_path = reports_dir / f"weekly-{stamp}.html"
    md_path.write_text(md, encoding="utf-8")
    html_path.write_text(html_text, encoding="utf-8")
    return md_path, html_path, m
