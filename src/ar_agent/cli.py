"""Command-line entry point.

Common commands:
    python -m ar_agent.cli matrix          show the tiering matrix (takes no action)
    python -m ar_agent.cli scan            run one daily scan
    python -m ar_agent.cli dashboard       open the web dashboard
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path

# The Windows console defaults to GBK, where Chinese text and emoji raise UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:                                       # noqa: BLE001
        pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ar_agent import report as report_mod                   # noqa: E402
from ar_agent import templates as tpl                       # noqa: E402
from ar_agent.audit import AuditLog                         # noqa: E402
from ar_agent.config import Config                          # noqa: E402
from ar_agent.models import ActionType, Message, Tier       # noqa: E402
from ar_agent.orchestrator import INBOX_COLUMNS, Orchestrator  # noqa: E402
from ar_agent.policy import rule_catalog                    # noqa: E402
from ar_agent.seed import seed as seed_data                 # noqa: E402

ROOT = Path(__file__).resolve().parents[2]

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
GREEN, YELLOW, RED, BLUE, GREY = ("\033[32m", "\033[33m", "\033[31m", "\033[36m", "\033[90m")

TIER_STYLE = {
    "low": (GREEN, "🟢 Light"),
    "mid": (YELLOW, "🟡 Medium"),
    "high": (RED, "🔴 High"),
    "none": (GREY, "—  none"),
}


def _enable_ansi() -> None:
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            k.SetConsoleMode(k.GetStdHandle(-11), 7)
        except Exception:                                   # noqa: BLE001
            pass


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise SystemExit(f"cannot parse date: {s} (use YYYY-MM-DD or 'YYYY-MM-DD HH:MM')")


def _orc() -> Orchestrator:
    return Orchestrator(ROOT)


def _rule(char: str = "─", n: int = 88) -> str:
    return GREY + char * n + RESET


def _w(text: str) -> int:
    """Terminal display width: CJK characters take 2 columns, everything else 1.

    Using len() would misalign every table that contains Chinese customer names.
    """
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _pad(text: str, width: int, align: str = "<") -> str:
    """Pad to a display width (f-string :<n counts characters, not columns)."""
    gap = max(width - _w(text), 0)
    if align == ">":
        return " " * gap + text
    return text + " " * gap


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def cmd_seed(args) -> None:
    anchor = (_parse_dt(args.today) or datetime.now()).date()
    stats = seed_data(ROOT / "data", anchor)
    print(f"{GREEN}✓{RESET} Demo data generated (anchor day {anchor})")
    print(f"  {stats['customers']} customers · {stats['invoices']} invoices · {stats['replies']} customer replies")
    print(f"  {DIM}data/customers.csv · data/receivables.csv · data/inbox.csv{RESET}")
    print(f"\n  Next: {BOLD}python -m ar_agent.cli matrix{RESET}")


def cmd_matrix(args) -> None:
    """Read-only dry plan: show how each invoice would be tiered and why, without taking any action."""
    orc = _orc()
    now = _parse_dt(args.date) or datetime.now()
    rep = orc.daily_scan(now=now, dry_plan=True)

    print(f"\n{BOLD}Tiering decision matrix{RESET}  {DIM}(dry plan: no messages sent, no state written){RESET}")
    print(f"{DIM}Scan time: {now:%Y-%m-%d %H:%M}{RESET}\n")
    print(_rule())
    print(BOLD + _pad("Invoice", 11) + _pad("Customer", 12) + _pad("Amount", 12, ">")
          + "  " + _pad("Overdue", 7, ">") + "  " + _pad("Tier", 10) + "Action" + RESET)
    print(_rule())

    order = {"high": 0, "mid": 1, "low": 2, "none": 3}
    for d in sorted(rep.actions, key=lambda x: (order[x.tier.value], -x.invoice.amount)):
        color, label = TIER_STYLE[d.tier.value]
        days = d.detail.get("days_overdue", 0)
        days_s = f"{days}d" if days >= 1 else "not due"
        if not d.invoice.is_open():
            days_s = "settled"
        moved = "" if d.tier is d.base_tier else f"  {DIM}(was {TIER_STYLE[d.base_tier.value][1]}){RESET}"
        print(_pad(d.invoice.invoice_id, 11)
              + _pad(d.customer.name, 12)
              + _pad("¥" + format(d.invoice.amount, ",.0f"), 12, ">")
              + "  " + _pad(days_s, 7, ">")
              + "  " + color + _pad(label, 10) + RESET
              + _pad(_action_label(d.action), 20) + moved)
        if args.verbose or d.policy_hits:
            for r in d.reasons:
                print(f"    {GREY}· {r}{RESET}")
            for h in d.policy_hits:
                print(f"    {RED}⚑ {h}{RESET}")
    print(_rule())

    counts = rep.by_tier
    print(f"\n{rep.scanned} invoices   "
          f"{GREEN}🟢 light {counts.get('low', 0)}{RESET}   "
          f"{YELLOW}🟡 medium {counts.get('mid', 0)}{RESET}   "
          f"{RED}🔴 high {counts.get('high', 0)}{RESET}   "
          f"{GREY}none {counts.get('none', 0)}{RESET}")

    forced = [d for d in rep.actions if d.tier is not d.base_tier]
    if forced:
        print(f"\n{BOLD}Invoices forced up by the L4 policy layer ({len(forced)}){RESET}"
              f"   {DIM}(the tiering engine rated them lower; the policy layer overrode it){RESET}")
        for d in forced:
            print(f"  {d.invoice.invoice_id}  {d.customer.name}  "
                  f"{DIM}{d.base_tier.value} → {RESET}{RED}{d.tier.value}{RESET}")
            for h in d.policy_hits:
                print(f"      {RED}⚑{RESET} {h}")


def _action_label(a: ActionType) -> str:
    return {
        ActionType.WHATSAPP_REMINDER: "WhatsApp reminder",
        ActionType.GMAIL_DUNNING: "dunning email",
        ActionType.SLACK_AM_NOTICE: "Slack to account manager",
        ActionType.SLACK_FINANCE_ALERT: "Slack alert to finance",
        ActionType.GMAIL_FINANCE_ALERT: "email alert to finance",
        ActionType.ESCALATE: "escalate to a person (no customer contact)",
        ActionType.HOLD: "hold",
        ActionType.SKIP: "skip",
    }.get(a, a.value)


def cmd_scan(args) -> None:
    orc = _orc()
    now = _parse_dt(args.date) or datetime.now()
    live = any(orc.channels[c].live for c in orc.channels)

    print(f"\n{BOLD}Daily scan{RESET}  {DIM}{now:%Y-%m-%d %H:%M}{RESET}")
    if live:
        print(f"{RED}⚠ LIVE mode: this run really sends messages to customers and colleagues{RESET}")
    else:
        print(f"{DIM}dry-run mode: messages are written to data/outbox/ and not actually sent{RESET}")
    print()

    rep = orc.daily_scan(now=now)

    acted = [d for d in rep.actions if d.action not in (ActionType.SKIP, ActionType.HOLD)]
    for d in acted:
        color, label = TIER_STYLE[d.tier.value]
        print(f"  {color}●{RESET} " + _pad(d.invoice.invoice_id, 11)
              + _pad(d.customer.name, 12)
              + color + _pad(label, 10) + RESET
              + "→  " + _action_label(d.action))
        for h in d.policy_hits:
            print(f"      {RED}⚑{RESET} {h}")

    held = [d for d in rep.actions if d.action is ActionType.HOLD]
    if held:
        print(f"\n  {DIM}On hold: {len(held)}{RESET}")
        for d in held[:8]:
            # The policy reason explains "why it is on hold" better than the tier reason, so show it first
            reason = d.policy_hits[-1] if d.policy_hits else (d.reasons[-1] if d.reasons else "")
            mark = f"{RED}⚑{RESET} " if d.policy_hits else ""
            print(f"    {GREY}○ " + _pad(d.invoice.invoice_id, 11) + _pad(d.customer.name, 12)
                  + RESET + mark + f"{GREY}{reason}{RESET}")
        if len(held) > 8:
            print(f"    {GREY}… and {len(held) - 8} more{RESET}")

    print(f"\n{_rule()}")
    ok = sum(1 for _, _, s in rep.sends if s)
    print(f"Scanned {rep.scanned}   actions {len(acted)}   messages sent {ok}/{len(rep.sends)}"
          f"   escalated to a person {len(rep.escalations)}")
    if rep.inbound_processed:
        print(f"Customer replies processed: {rep.inbound_processed}")
    if rep.deferred:
        print(f"{YELLOW}Breaker: {rep.deferred} invoices deferred to the next batch{RESET}")
    if rep.halted:
        print(f"{RED}Breaker: {rep.halt_reason}{RESET}")
    print(f"{DIM}run_id={rep.run_id} · full record in data/action_log.csv{RESET}")
    if not live and rep.sends:
        print(f"{DIM}Message text is in data/outbox/ (see it with: python -m ar_agent.cli outbox){RESET}")


def cmd_takeover(args) -> None:
    _orc().takeover(args.invoice_id, args.by, args.note or "")
    print(f"{GREEN}✓{RESET} {args.invoice_id} taken over by {BOLD}{args.by}{RESET}; the agent stops all automatic actions on it")


def cmd_release(args) -> None:
    _orc().release(args.invoice_id, args.by)
    print(f"{GREEN}✓{RESET} {args.invoice_id} handed back to the agent for follow-up")


def cmd_paid(args) -> None:
    on = (_parse_dt(args.on) or datetime.now()).date()
    _orc().mark_paid(args.invoice_id, on)
    print(f"{GREEN}✓{RESET} {args.invoice_id} marked as paid ({on})")


def cmd_reply(args) -> None:
    """Simulate a customer reply, to demonstrate forced keyword escalation."""
    from ar_agent.inbound import classify_reply

    orc = _orc()
    rows = orc._read_inbox()
    inv = next((i for i in orc.store.load_invoices() if i.invoice_id == args.invoice_id), None)
    if inv is None:
        raise SystemExit(f"invoice not found: {args.invoice_id}")
    when = _parse_dt(args.at) or datetime.now()
    rows.append({
        "reply_id": f"R-{len(rows) + 1:03d}", "invoice_id": args.invoice_id,
        "customer_id": inv.customer_id, "received_at": when.isoformat(timespec="seconds"),
        "channel": args.channel, "text": args.text, "processed": "", "intent": "",
    })
    orc._write_inbox(rows)

    a = classify_reply(args.text)
    print(f"{GREEN}✓{RESET} Recorded a customer reply for {args.invoice_id}")
    print(f"  {DIM}Text: {args.text}{RESET}")
    print(f"  Intent: {BOLD}{a.intent.label}{RESET}"
          + (f"   matched keywords: {', '.join(a.matched)}" if a.matched else ""))
    if a.forces_escalation:
        print(f"  {RED}⚑ Forced-escalation category matched: on the next scan this invoice becomes high risk and automation stops{RESET}")


def cmd_log(args) -> None:
    log = AuditLog(ROOT / "data" / "action_log.csv").read_all()
    if args.invoice:
        log = [r for r in log if r.get("invoice_id") == args.invoice]
    if args.actor:
        log = [r for r in log if r.get("actor") == args.actor]
    if not args.all:
        log = [r for r in log if r.get("action") not in ("hold", "skip")]
    log = log[-args.limit:]

    if not log:
        print(f"{DIM}No matching records{RESET}")
        return

    print(f"\n{BOLD}Audit log{RESET}  {DIM}L5 observability: every record answers who acted, on whom, and why{RESET}\n")
    print(_rule())
    print(BOLD + _pad("Time", 18) + _pad("Who", 9) + _pad("Invoice", 11)
          + _pad("Action", 24) + _pad("Channel", 16) + "Mode" + RESET)
    print(_rule())
    for r in log:
        actor = r.get("actor", "")
        tag = {"agent": BLUE + _pad("Agent", 9) + RESET,
               "human": YELLOW + _pad("human", 9) + RESET,
               "system": GREY + _pad("system", 9) + RESET}.get(actor, _pad(actor, 9))
        print(_pad(r.get("timestamp", "")[:16], 18) + tag
              + _pad(r.get("invoice_id", "") or "-", 11)
              + _pad(r.get("action", ""), 24)
              + _pad(r.get("channel", "") or "-", 16)
              + (r.get("mode", "") or "-"))
        if args.verbose:
            if r.get("reasons"):
                print(f"    {GREY}· {r['reasons']}{RESET}")
            if r.get("policy_hits"):
                print(f"    {RED}⚑ {r['policy_hits']}{RESET}")
    print(_rule())
    agent_n = sum(1 for r in log if r.get("actor") == "agent")
    human_n = sum(1 for r in log if r.get("actor") == "human")
    print(f"{len(log)} records   {BLUE}agent {agent_n}{RESET}   {YELLOW}human {human_n}{RESET}"
          f"   {GREY}system {len(log) - agent_n - human_n}{RESET}")


def cmd_outbox(args) -> None:
    files = sorted((ROOT / "data" / "outbox").glob("*.json"))
    if args.channel:
        files = [f for f in files if f"_{args.channel}_" in f.name]
    files = files[-args.limit:]
    if not files:
        print(f"{DIM}The outbox is empty. Run a scan first.{RESET}")
        return
    for f in files:
        d = json.loads(f.read_text(encoding="utf-8"))
        aud = "to customer" if d["audience"] == "customer" else "internal"
        color = RED if d["audience"] == "customer" else BLUE
        print(f"\n{_rule('═')}")
        print(f"{color}[{d['channel']} · {aud}]{RESET} → {BOLD}{d['to']}{RESET}"
              f"   {DIM}template {d['template_id']} · {d['invoice_id']} · {d['mode']}{RESET}")
        if d["subject"]:
            print(f"{BOLD}Subject: {d['subject']}{RESET}")
        print(_rule("─"))
        print(d["body"])
    print(f"\n{_rule('═')}")
    print(f"{DIM}{len(files)} messages (all dry-run, none actually sent){RESET}")


def cmd_report(args) -> None:
    orc = _orc()
    as_of = (_parse_dt(args.date) or datetime.now()).date()
    md_path, html_path, m = report_mod.generate(ROOT, orc.cfg, as_of)

    print(f"\n{GREEN}✓{RESET} Weekly report generated")
    print(f"  {md_path.relative_to(ROOT)}")
    print(f"  {html_path.relative_to(ROOT)}  {DIM}<- open in a browser{RESET}\n")
    print(_rule())
    print(f"Open receivables {BOLD}¥{m.total_open_amount:,.0f}{RESET}"
          f"   of which overdue {RED}¥{m.overdue_amount:,.0f}{RESET} ({m.overdue_count} invoices)")
    print(f"DSO proxy {m.dso_proxy:.1f} days   overdue coverage {m.coverage_rate:.0f}%")
    print(f"Agent actions {m.agent_actions}   human actions {m.human_actions}")
    print(f"Guardrails: VIPs chased automatically {m.vip_auto_contacted} (target 0)   "
          f"wrong escalations {m.wrong_escalations} (target 0)")
    print(_rule())

    if args.send:
        subject = f"[Weekly report] Accounts receivable health · {as_of}"
        body = md_path.read_text(encoding="utf-8")
        msg = Message("gmail_internal", orc.cfg.company["cfo_email"], subject, body,
                      "WEEKLY_REPORT", "", "", "internal")
        res = orc.channels["gmail_internal"].send(msg)
        orc.audit.write(run_id="report", actor="agent", action="weekly_report",
                        channel="gmail_internal", recipient=orc.cfg.company["cfo_email"],
                        mode=res.mode, ok="1" if res.ok else "0",
                        reasons=[f"weekly report {as_of}"])
        print(f"\n{GREEN}✓{RESET} Delivered to the CFO ({res.mode}): {orc.cfg.company['cfo_email']}")


def cmd_rules(args) -> None:
    print(f"\n{BOLD}L4 runtime policy layer{RESET}  {DIM}hard rules outside the LLM; they act on a match and the agent cannot bypass them{RESET}\n")
    for r in rule_catalog():
        print(f"  {RED}{r['id']}{RESET}  {r['description']}")

    print(f"\n{BOLD}L2 pre-approved templates{RESET}  {DIM}the agent can only pick a template and fill structured variables; no free-text input{RESET}\n")
    print("  " + DIM + _pad("Template ID", 24) + _pad("Channel", 18) + "Audience" + RESET)
    for t in tpl.catalog():
        color = RED if t["audience"] == "customer" else BLUE
        aud = "to customer" if t["audience"] == "customer" else "internal"
        print("  " + _pad(t["id"], 24) + _pad(t["channel"], 18) + color + aud + RESET)
        print(f"    {GREY}{t['note']}{RESET}")

    print(f"\n{BOLD}Pre-send content gate{RESET}  {DIM}customer templates are scanned for forbidden wording after rendering{RESET}\n")
    for _, why in tpl.FORBIDDEN_PATTERNS:
        print(f"  {GREY}·{RESET} {why}")

    orc = _orc()
    print(f"\n{BOLD}L3 agent-writable field allow-list{RESET}  {DIM}amount, terms and contact details are excluded; an unauthorised write raises{RESET}\n")
    print(f"  {GREEN}writable{RESET}: {', '.join(sorted(orc.store.AGENT_WRITABLE_FIELDS))}")
    print(f"  {RED}read-only{RESET}: invoice_id, customer_id, amount, issue_date, due_date, status, paid_date")


def cmd_reset_breaker(args) -> None:
    orc = _orc()
    orc.breaker.reset(args.channel)
    target = args.channel or "all channels"
    print(f"{GREEN}✓{RESET} Breaker reset: {target}")


def cmd_verify(args) -> None:
    """Self-check: run every risk boundary one by one and report pass/fail."""
    from ar_agent.verify import run_checks
    run_checks(ROOT)


def cmd_ask(args) -> None:
    """Ask the read-only assistant a question; it answers by calling read-only tools."""
    from ar_agent.assistant import Assistant
    question = " ".join(args.question)
    now = _parse_dt(args.date) or datetime.now()
    res = Assistant(ROOT, Config.load(ROOT / "config.json"), now=now).ask(question)
    if res.source == "disabled":
        raise SystemExit(f"{res.note}\n  e.g. PowerShell: $env:AR_LLM_PROVIDER='ollama'; $env:AR_LLM_MODEL='qwen3.5:9b'")
    print(f"\n{BOLD}Q:{RESET} {question}\n")
    if res.source == "llm":
        print(res.answer)
    else:
        print(f"{YELLOW}{res.note}{RESET}")
        if res.answer:
            print(f"{DIM}Data retrieved:{RESET}\n{res.answer}")
    def fmt(c) -> str:
        shown = ", ".join(f"{k}={v}" for k, v in c.args.items()) if isinstance(c.args, dict) else str(c.args or "")
        return f"{GREEN if c.ok else RED}{c.name}{RESET}({shown})"

    if res.calls:
        print(f"\n{DIM}tools used:{RESET} " + "  ".join(fmt(c) for c in res.calls))
    print(f"{DIM}read-only: the assistant has no tool that sends, writes or marks anything{RESET}\n")


def cmd_demo(args) -> None:
    from ar_agent.demo import run_demo
    run_demo(ROOT, pause=not args.no_pause)


def cmd_dashboard(args) -> None:
    from ar_agent.dashboard import serve
    serve(ROOT, args.port, open_browser=not args.no_open)


# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ar", description="AR collections agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Typical flow:
  python -m ar_agent.cli seed              generate demo data
  python -m ar_agent.cli matrix -v         show tiering decisions (no actions)
  python -m ar_agent.cli scan              run one daily scan
  python -m ar_agent.cli outbox            see what the agent sent
  python -m ar_agent.cli report            generate the CFO weekly report
  python -m ar_agent.cli dashboard         open the web dashboard
  python -m ar_agent.cli ask "..."         ask the read-only assistant (needs AR_LLM_PROVIDER)
  python -m ar_agent.cli demo             run the full guided demo
""")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("seed", help="generate demo data")
    s.add_argument("--today", help="anchor day YYYY-MM-DD (default: today)")
    s.set_defaults(func=cmd_seed)

    s = sub.add_parser("matrix", help="dry-run the tiering without taking any action")
    s.add_argument("--date", help="simulated scan time")
    s.add_argument("-v", "--verbose", action="store_true", help="show the tier reasons for each invoice")
    s.set_defaults(func=cmd_matrix)

    s = sub.add_parser("scan", help="run the daily scan")
    s.add_argument("--date", help="simulated scan time, e.g. to play forward a few days")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("takeover", help="a person takes over an invoice; the agent stops at once")
    s.add_argument("invoice_id")
    s.add_argument("--by", required=True, help="who is taking over")
    s.add_argument("--note", help="note")
    s.set_defaults(func=cmd_takeover)

    s = sub.add_parser("release", help="hand an invoice back to the agent")
    s.add_argument("invoice_id")
    s.add_argument("--by", required=True)
    s.set_defaults(func=cmd_release)

    s = sub.add_parser("paid", help="mark an invoice as paid (human action)")
    s.add_argument("invoice_id")
    s.add_argument("--on", help="payment date")
    s.set_defaults(func=cmd_paid)

    s = sub.add_parser("reply", help="simulate a customer reply")
    s.add_argument("invoice_id")
    s.add_argument("--text", required=True)
    s.add_argument("--channel", default="whatsapp")
    s.add_argument("--at", help="reply time")
    s.set_defaults(func=cmd_reply)

    s = sub.add_parser("log", help="show the audit log")
    s.add_argument("--invoice")
    s.add_argument("--actor", choices=["agent", "human", "system"])
    s.add_argument("--limit", type=int, default=40)
    s.add_argument("--all", action="store_true", help="include hold/skip records")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(func=cmd_log)

    s = sub.add_parser("outbox", help="show the full text of messages the agent sent")
    s.add_argument("--limit", type=int, default=5)
    s.add_argument("--channel", choices=["whatsapp", "gmail", "gmail_internal", "slack"])
    s.set_defaults(func=cmd_outbox)

    s = sub.add_parser("report", help="generate the CFO weekly report")
    s.add_argument("--date")
    s.add_argument("--send", action="store_true", help="also deliver it to the CFO")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("rules", help="show the risk-boundary design (L2/L3/L4)")
    s.set_defaults(func=cmd_rules)

    s = sub.add_parser("verify", help="self-check: verify each risk boundary really holds")
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("reset-breaker", help="reset a circuit breaker")
    s.add_argument("--channel")
    s.set_defaults(func=cmd_reset_breaker)

    s = sub.add_parser("demo", help="run the full guided demo")
    s.add_argument("--no-pause", action="store_true", help="run straight through without waiting for Enter")
    s.set_defaults(func=cmd_demo)

    s = sub.add_parser("ask", help="ask the read-only assistant a question (LLM with read-only tools)")
    s.add_argument("question", nargs="+", help='e.g. "Which high-risk invoices are over 30 days overdue?"')
    s.add_argument("--date", help="answer as of this scan time")
    s.set_defaults(func=cmd_ask)

    s = sub.add_parser("dashboard", help="start the web dashboard")
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--no-open", action="store_true")
    s.set_defaults(func=cmd_dashboard)

    return p


def main(argv: list[str] | None = None) -> int:
    _enable_ansi()
    args = build_parser().parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
