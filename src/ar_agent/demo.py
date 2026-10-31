"""Guided demo: the whole story line in one command.

The full flow is fixed in a script; each step first says what it is checking, then runs and shows the result.

13 scenes, about 5-8 minutes, covering: tiering -> automatic follow-up -> 48h buffer -> human takeover
-> forced keyword escalation -> zero customer messages on high risk -> circuit breakers -> weekly report -> risk-boundary self-check.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
GREEN, YELLOW, RED, BLUE, GREY = ("\033[32m", "\033[33m", "\033[31m", "\033[36m", "\033[90m")

ROOT: Path = Path(".")
PAUSE = True
STEP = 0


def _run(*args: str) -> None:
    """Run one CLI command with its output going straight to the terminal (colours kept).

    Flush first: the child process writes to the terminal directly, while this process's print is block-buffered under a pipe.
    Without the flush, all the narration would pile up at the end and the demo would lose its pacing.
    """
    sys.stdout.flush()
    subprocess.run([sys.executable, str(ROOT / "ar.py"), *args], check=False)
    sys.stdout.flush()


def scene(title: str, why: str) -> None:
    global STEP
    STEP += 1
    print(f"\n{GREY}{'═' * 88}{RESET}")
    print(f"{BOLD}Scene {STEP}   {title}{RESET}")
    print(f"{DIM}{why}{RESET}")
    print(f"{GREY}{'─' * 88}{RESET}")
    sys.stdout.flush()
    if PAUSE:
        try:
            input(f"{GREY}   Press Enter to continue…{RESET}")
        except (EOFError, KeyboardInterrupt):
            raise SystemExit(0)


def note(text: str) -> None:
    print(f"\n{BLUE}▸{RESET} {text}")


def show_outbox_one(match_invoice: str, label: str) -> None:
    """Pick one message from the outbox and print it as is."""
    files = sorted((ROOT / "data" / "outbox").glob("*.json"))
    for f in reversed(files):
        d = json.loads(f.read_text(encoding="utf-8"))
        if d.get("invoice_id") == match_invoice:
            out = d["audience"] == "customer"
            color = RED if out else BLUE
            print(f"\n{color}[{label}]{RESET} {d['channel']} → {BOLD}{d['to']}{RESET}"
                  f"   {DIM}template {d['template_id']}{RESET}")
            if d.get("subject"):
                print(f"{BOLD}Subject: {d['subject']}{RESET}")
            print(f"{GREY}{'┈' * 84}{RESET}")
            print(d["body"])
            print(f"{GREY}{'┈' * 84}{RESET}")
            sys.stdout.flush()
            return
    print(f"{DIM}(no message found for {match_invoice}){RESET}")


def run_demo(root: Path, pause: bool = True) -> None:
    global ROOT, PAUSE, STEP
    ROOT, PAUSE, STEP = Path(root), pause, 0

    anchor = "2026-08-25"

    print(f"\n{BOLD}AR Collections Agent · Demo{RESET}")
    print(f"{DIM}Simulated scenario: a B2B trading company, 300+ business customers, 30-60 day terms, 2 finance staff + 20 sales staff.")
    print(f"Demo anchor day {anchor}, dry-run throughout: nothing is sent to any real customer.{RESET}")

    # ---------------------------------------------------------------- #
    scene("Setup: 30 simulated invoices",
          "These 30 are not random: they are designed to hit every cell of the decision matrix and every policy rule, "
          "so you can point at any one of them and ask why it was tiered that way.")
    _run("seed", "--today", anchor)

    # ---------------------------------------------------------------- #
    scene("Tiering engine: 30 invoices, three risk tiers",
          "First the decisions alone, with no actions. Note the last section, invoices forced up by the L4 policy layer: "
          "the tiering engine rated them lower and the runtime policy layer made them stricter.")
    _run("matrix", "--date", anchor)

    # ---------------------------------------------------------------- #
    scene("First daily scan",
          "Light tier gets a WhatsApp message; medium first notifies the account manager on Slack and starts a 48-hour buffer; "
          "high risk only notifies the team and sends nothing to the customer.")
    _run("scan", "--date", f"{anchor} 09:00")

    # ---------------------------------------------------------------- #
    scene("What exactly did the agent say to the customer?",
          "The wording comes from a pre-approved template; the agent has no free-text input. Note the tone: professional, restrained, "
          "complete (amount / due date / payment link), with a clear 'ignore this if already paid' way out. (Customer messages are in Chinese.)")
    show_outbox_one("INV-2001", "to customer · first light-tier reminder")
    note("The agent cannot write something like 'we agree you may pay in instalments': "
         f"the render function {BOLD}render(template_id, variables){RESET} has no body parameter at all.")

    # ---------------------------------------------------------------- #
    scene("Medium: give people priority instead of acting before them",
          "This is where 'human-AI collaboration, not machine takeover' shows most clearly: the agent notifies the account manager first, "
          "tells them they have 48 hours of priority, and says what happens if they do not respond.")
    show_outbox_one("INV-2010", "internal · Slack notice to account manager")

    # ---------------------------------------------------------------- #
    scene("High risk: a misjudgement costs finance a second look, not a harmed customer",
          "High risk only triggers internal notifications, never a customer-facing action. This asymmetric risk control is deliberate: "
          "a false alarm adds some human review; a miss could let automation damage a customer relationship.")
    show_outbox_one("INV-2019", "internal · high-risk alert (260,000 / 40 days overdue)")
    note(f"Note the bold line '{BOLD}The system has sent nothing to this customer{RESET}': "
         "finance can confirm at a glance that the customer was not contacted.")

    # ---------------------------------------------------------------- #
    scene("Account manager takes over: the agent stops at once",
          "A takeover is an explicit command, not a Slack reply like 'I'll handle it'. "
          "Chat history is context, not authorisation; treating it as authorisation would let anyone who can post in that channel operate the system.")
    _run("takeover", "INV-2014", "--by", "李娜", "--note", "customer promised to pay this Friday; I will follow up myself")

    # ---------------------------------------------------------------- #
    scene("48 hours later: where the account manager did not respond, the agent steps in",
          "It sends a formal but restrained reconciliation email, not a pressure letter. "
          "It also records 'account manager did not respond' in the weekly report, which is a management signal in itself.")
    _run("scan", "--date", "2026-08-27 10:00")
    note("INV-2014, which was taken over, shows as 'hold': the agent respects the takeover and does not step on the person's work.")

    # ---------------------------------------------------------------- #
    scene("Key rule: when a customer mentions instalments, it goes to a person unconditionally",
          "Now simulate a customer reply on a light-tier invoice. The rule is plain keyword matching with no LLM: "
          "it does not judge whether the request is reasonable; a match escalates.")
    _run("reply", "INV-2005", "--text",
         "你好，这个月资金有点紧，我们能不能分三期把这笔款付清？",
         "--at", "2026-08-27 15:00")
    note("On the next scan this invoice jumps from 🟢 light straight to 🔴 high risk, and all automation stops.")

    # ---------------------------------------------------------------- #
    scene("Check: light -> high risk, and nothing more is sent to the customer",
          "By days and amount the tiering engine rated it light; the runtime policy layer overrode that. "
          "This is how 'complex logic that cannot be bypassed' works in practice: the rule layer decides, not the model.")
    _run("scan", "--date", "2026-08-28 09:00")
    note("A few more days on: INV-2026 has only just passed its due date. It is the first invoice to go overdue "
         "after go-live, and only invoices like it measure the real time to first touch.")
    _run("scan", "--date", "2026-08-31 09:00")

    # ---------------------------------------------------------------- #
    scene("The CFO's weekly report",
          "The CFO wants numbers without having to chase finance. Note part three, the guardrail metrics: "
          "counter-metrics only really constrain decisions when they sit in a report the CFO reads every week; "
          "hidden in a monitoring back end, nobody takes them seriously.")
    _run("report", "--date", "2026-08-31", "--send")

    # ---------------------------------------------------------------- #
    scene("Risk-boundary self-check: verified on the spot, not promised",
          "Anyone can write 'we have 7 layers of defence' in a document. Every check below "
          "deliberately does something that should not be allowed, then asserts that it was stopped.")
    _run("verify")

    # ---------------------------------------------------------------- #
    scene("Web dashboard",
          "The four questions finance and the CFO actually ask: how much money is outstanding, "
          "why each invoice was tiered as it was, what the agent said to customers, and whether efficiency cost us customer relationships.")
    print(f"\n  Dashboard: {BOLD}http://127.0.0.1:8787/{RESET}")
    print(f"  {DIM}Start it with: python ar.py dashboard{RESET}")
    print(f"\n{GREY}{'═' * 88}{RESET}")
    print(f"{GREEN}{BOLD}Demo finished{RESET}   {DIM}dry-run throughout; "
          f"every message is in data/outbox/ and nothing was sent to a real customer.{RESET}")
    print(f"{DIM}Generated {datetime.now():%Y-%m-%d %H:%M}{RESET}\n")
