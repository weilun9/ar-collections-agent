"""Daily scan pipeline (product spec §3.1).

The full path of one invoice through one scan:

    read -> [breaker: batch cap] -> tiering engine (§4) -> inbox intent (§4.2)
         -> L4 policy chain -> action decision -> [breaker: send failures] -> channel send -> write back state -> audit log

About the word "agent": the **decision part of this pipeline is entirely deterministic code**, with no LLM call at all.
The LLM is an optional extra (a contact-history summary for high-risk alerts; see summarize_history / summarizer.py),
and even if it is completely unavailable, collection still runs and tiering is still correct. This is a deliberate architecture choice:
keep the parts that must not fail apart from the parts where a failure only makes things less pleasant to read.
"""
from __future__ import annotations

import csv
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from . import policy as policy_mod
from . import templates as tpl
from .audit import AuditLog
from .breaker import CircuitBreaker
from .channels import build_registry
from .config import Config
from .inbound import ReplyIntent, classify_reply
from .models import (
    ActionType,
    Actor,
    AgentState,
    Customer,
    Decision,
    Invoice,
    Message,
    Tier,
)
from .store import CsvStore
from .summarizer import Summarizer

INBOX_COLUMNS = ["reply_id", "invoice_id", "customer_id", "received_at",
                 "channel", "text", "processed", "intent"]


@dataclass
class ScanReport:
    run_id: str
    run_at: datetime
    scanned: int = 0
    deferred: int = 0                 # deferred to the next batch by the batch cap
    by_tier: dict[str, int] = field(default_factory=dict)
    actions: list[Decision] = field(default_factory=list)
    sends: list[tuple[str, str, bool]] = field(default_factory=list)
    halted: bool = False
    halt_reason: str = ""
    inbound_processed: int = 0
    escalations: list[str] = field(default_factory=list)

    def count(self, tier: Tier) -> None:
        self.by_tier[tier.value] = self.by_tier.get(tier.value, 0) + 1


class Orchestrator:
    def __init__(self, root: Path, cfg: Config | None = None):
        self.root = Path(root)
        self.cfg = cfg or Config.load(self.root / "config.json")
        self.data_dir = self.root / "data"
        self.store = CsvStore(self.data_dir)
        self.audit = AuditLog(self.data_dir / "action_log.csv")
        self.breaker = CircuitBreaker(self.data_dir / "breaker_state.json", self.cfg)
        self.channels = build_registry(self.cfg, self.data_dir / "outbox")
        self.inbox_path = self.data_dir / "inbox.csv"
        self.summarizer = Summarizer(self.cfg)

    # ------------------------------------------------------------------ #
    # Inbox
    # ------------------------------------------------------------------ #
    def _read_inbox(self) -> list[dict[str, str]]:
        if not self.inbox_path.exists():
            return []
        with open(self.inbox_path, encoding="utf-8-sig", newline="") as fh:
            return list(csv.DictReader(fh))

    def _write_inbox(self, rows: list[dict[str, str]]) -> None:
        with open(self.inbox_path, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=INBOX_COLUMNS)
            w.writeheader()
            for r in rows:
                w.writerow({c: r.get(c, "") for c in INBOX_COLUMNS})

    def _pending_intents(self, upto: datetime) -> dict[str, tuple[str, str]]:
        """Scan customer replies and return {invoice_id: (intent, summary)}.

        Only replies up to `upto` are used, so replaying history with --date never sees messages from the future.
        When an invoice has several replies, the highest-risk one wins (TERMS_CHANGE > DISPUTE > ...).
        """
        result: dict[str, tuple[str, str]] = {}
        best_rank: dict[str, int] = {}
        order = {i.value: n for n, i in enumerate(reversed(
            [ReplyIntent.UNKNOWN, ReplyIntent.ACK, ReplyIntent.PAID_CLAIM,
             ReplyIntent.DISPUTE, ReplyIntent.TERMS_CHANGE]))}

        for row in self._read_inbox():
            received = (row.get("received_at") or "").strip()
            if received:
                try:
                    if datetime.fromisoformat(received) > upto:
                        continue
                except ValueError:
                    pass
            analysis = classify_reply(row.get("text", ""))
            if analysis.intent is ReplyIntent.UNKNOWN:
                continue
            inv = row["invoice_id"].strip()
            rank = order.get(analysis.intent.value, 0)
            if rank >= best_rank.get(inv, -1):
                best_rank[inv] = rank
                result[inv] = (analysis.intent.value, analysis.summary())
        return result

    def process_inbox(self, upto: datetime | None = None) -> int:
        """Tag unprocessed inbox replies with their intent and save them (for the dashboard and audits)."""
        upto = upto or datetime.now()
        rows = self._read_inbox()
        n = 0
        for row in rows:
            if (row.get("processed") or "").strip() in ("1", "true", "yes"):
                continue
            received = (row.get("received_at") or "").strip()
            if received:
                try:
                    if datetime.fromisoformat(received) > upto:
                        continue
                except ValueError:
                    pass
            analysis = classify_reply(row.get("text", ""))
            row["intent"] = analysis.intent.value
            row["processed"] = "1"
            n += 1
        if n:
            self._write_inbox(rows)
        return n

    # ------------------------------------------------------------------ #
    # Decisions
    # ------------------------------------------------------------------ #
    def decide(
        self,
        inv: Invoice,
        cust: Customer,
        now: datetime,
        intents: dict[str, tuple[str, str]],
        sends_today: int,
    ) -> Decision:
        from .tiering import classify

        today = now.date()
        tier_res = classify(inv, cust, self.cfg, today)

        intent, intent_summary = intents.get(inv.invoice_id, (None, ""))
        ctx = policy_mod.PolicyContext(
            invoice=inv, customer=cust, base_tier=tier_res.tier, today=today,
            sends_today=sends_today, inbound_intent=intent, inbound_summary=intent_summary,
        )
        outcome = policy_mod.evaluate(ctx, self.cfg)

        d = Decision(
            invoice=inv, customer=cust, tier=outcome.tier, base_tier=tier_res.tier,
            action=ActionType.SKIP, reasons=list(tier_res.reasons),
            policy_hits=list(outcome.hits),
            agent_may_contact_customer=outcome.agent_may_contact_customer,
            detail={"days_overdue": tier_res.days_overdue, "halt": outcome.halt_all_sending},
        )

        # ---- Final states: settled / taken over by a person / escalated; the agent does nothing ----
        if not inv.is_open():
            d.action = ActionType.SKIP
            d.add_reason("invoice settled")
            return d

        state = AgentState(inv.agent_state) if inv.agent_state else AgentState.NEW
        if state in (AgentState.HUMAN_TAKEN, AgentState.ESCALATED):
            d.action = ActionType.HOLD
            d.add_reason(
                "taken over by a person; the agent stops all actions"
                if state is AgentState.HUMAN_TAKEN
                else "escalated to a person; the agent no longer contacts the customer"
            )
            return d

        if d.tier is Tier.NONE:
            d.action = ActionType.SKIP
            return d

        # ---- 🔴 High risk: notify people only, never contact the customer ----
        if d.tier is Tier.HIGH:
            d.action = ActionType.ESCALATE
            return d

        # ---- Policy layer forbids customer contact (VIP / says it has paid / daily send cap, etc.) ----
        if not d.agent_may_contact_customer:
            d.action = ActionType.ESCALATE if outcome.tier is Tier.HIGH else ActionType.HOLD
            return d

        # ---- 🟡 Medium: 48-hour buffer state machine ----
        if d.tier is Tier.MID:
            buffer_hours = self.cfg.cadence["mid_am_buffer_hours"]
            if state is not AgentState.AWAITING_AM or not inv.agent_buffer_until:
                d.action = ActionType.SLACK_AM_NOTICE
                d.detail["buffer_until"] = (now + timedelta(hours=buffer_hours)).isoformat(timespec="seconds")
                d.detail["buffer_hours"] = buffer_hours
                d.add_reason(f"notified account manager {cust.account_manager}; {buffer_hours} hours of priority to act first")
                return d

            deadline = datetime.fromisoformat(inv.agent_buffer_until)
            if now < deadline:
                d.action = ActionType.HOLD
                remaining = deadline - now
                hrs = remaining.total_seconds() / 3600
                d.add_reason(f"inside the account manager's buffer, {hrs:.1f} hours left; the agent waits")
                return d

            d.action = ActionType.GMAIL_DUNNING
            d.add_reason(f"account manager {cust.account_manager} did not respond within {buffer_hours} hours; the agent steps in with a formal reconciliation email")
            d.detail["am_unresponsive"] = True
            return d

        # ---- 🟢 Light: automatic follow-up on a cadence ----
        cad = self.cfg.cadence
        if inv.agent_stage >= cad["low_max_reminders"]:
            d.action = ActionType.HOLD
            d.add_reason(
                f"{inv.agent_stage} reminders sent, the light-tier maximum of "
                f"{cad['low_max_reminders']}; if it stays overdue it will move up to medium"
            )
            return d

        if inv.agent_stage == 0:
            d.action = ActionType.WHATSAPP_REMINDER
            d.detail["template_id"] = "WA_LOW_1"
            d.add_reason("first automatic reminder")
            return d

        gap = cad["low_second_reminder_gap_days"]
        last = inv.agent_last_action_at
        if last:
            try:
                since = (now - datetime.fromisoformat(last)).days
            except ValueError:
                since = gap
            if since < gap:
                d.action = ActionType.HOLD
                d.add_reason(f"{since} days since the last reminder; the {gap}-day gap has not passed")
                return d

        d.action = ActionType.WHATSAPP_REMINDER
        d.detail["template_id"] = "WA_LOW_2"
        d.add_reason(f"{gap} days since the last reminder; sending reminder 2")
        return d

    # ------------------------------------------------------------------ #
    # Execution
    # ------------------------------------------------------------------ #
    def _vars(self, d: Decision, now: datetime) -> dict:
        inv, cust = d.invoice, d.customer
        return {
            "company": self.cfg.company["name"],
            "contact_name": cust.contact_name,
            "customer_name": cust.name,
            "account_manager": cust.account_manager,
            "invoice_id": inv.invoice_id,
            "amount": f"{inv.amount:,.2f}",
            "issue_date": inv.issue_date.isoformat() if inv.issue_date else "",
            "due_date": inv.due_date.isoformat() if inv.due_date else "",
            "days_overdue": d.detail.get("days_overdue", 0),
            "payment_url": self.cfg.company["payment_portal_url"] + inv.invoice_id,
            "reasons": "; ".join(d.reasons) or "—",
            "policy_hits": "; ".join(d.policy_hits) or "none",
            "history": self.summarize_history(inv.invoice_id),
            "buffer_hours": d.detail.get("buffer_hours", self.cfg.cadence["mid_am_buffer_hours"]),
            "buffer_until": d.detail.get("buffer_until", ""),
            "timestamp": now.isoformat(timespec="seconds"),
            "reason": d.detail.get("halt_reason", ""),
            "channel": d.detail.get("channel", ""),
        }

    def history_facts(self, invoice_id: str) -> list[str]:
        """Deterministic list of contact facts (last 5 actions + 3 customer replies), in time order."""
        rows = [r for r in self.audit.read_all() if r.get("invoice_id") == invoice_id]
        replies = [r for r in self._read_inbox() if r.get("invoice_id") == invoice_id]
        parts = []
        for r in rows[-5:]:
            if r.get("action") in ("hold", "skip"):
                continue
            who = "agent" if r.get("actor") == "agent" else "person"
            parts.append(f"{r.get('timestamp', '')[:16]} {who} via {r.get('channel') or '-'}: {r.get('action')}")
        for r in replies[-3:]:
            txt = (r.get("text") or "").replace("\n", " ")[:60]
            parts.append(f"{(r.get('received_at') or '')[:16]} customer replied: \"{txt}\"")
        return sorted(parts)

    def summarize_history(self, invoice_id: str) -> str:
        """Contact-history summary attached to high-risk alerts for finance: the only LLM call in the whole agent.

        With llm.provider configured, a model condenses it into a paragraph and the raw records are kept for checking;
        if it is not configured, the call fails or the summary fails the check, it falls back to the deterministic list. See summarizer.py.
        """
        result = self.summarizer.summarize(self.history_facts(invoice_id))
        if result.note:
            return f"{result.text}\n  ({result.note})"
        return result.text

    _ACTION_PLAN: dict[ActionType, tuple[str, str]] = {
        ActionType.WHATSAPP_REMINDER: ("whatsapp", ""),          # template chosen in decide()
        ActionType.GMAIL_DUNNING: ("gmail", "MAIL_MID_DUNNING"),
        ActionType.SLACK_AM_NOTICE: ("slack", "SLACK_AM_BUFFER"),
    }

    def execute(self, d: Decision, report: ScanReport, now: datetime) -> None:
        inv, cust = d.invoice, d.customer
        updates: dict[str, object] = {"agent_tier": d.tier.value}

        def log(action: str, channel: str = "", recipient: str = "",
                template_id: str = "", mode: str = "", ok: str = "",
                preview: str = "", actor: str = Actor.AGENT.value) -> None:
            self.audit.write(
                timestamp=now.isoformat(timespec="seconds"), run_id=report.run_id, actor=actor,
                invoice_id=inv.invoice_id, customer_id=cust.customer_id, customer_name=cust.name,
                tier=d.tier.value, base_tier=d.base_tier.value, action=action, channel=channel,
                recipient=recipient, template_id=template_id, mode=mode, ok=ok,
                reasons=d.reasons, policy_hits=d.policy_hits, content_preview=preview,
            )

        def deliver(channel_key: str, template_id: str, to: str) -> bool:
            channel = self.channels[channel_key]
            if self.breaker.is_tripped(channel.name):
                log(d.action.value, channel.name, to, template_id, "blocked", "0",
                    "channel breaker tripped; skipped")
                return False
            variables = self._vars(d, now)
            subject, body = tpl.render(template_id, variables)
            msg = Message(
                channel=channel.name, to=to, subject=subject, body=body,
                template_id=template_id, invoice_id=inv.invoice_id,
                customer_id=cust.customer_id, audience=channel.audience,
                variables=variables,
            )
            res = channel.send(msg)
            tripped = self.breaker.record_send(channel.name, res.ok, now.date())
            report.sends.append((channel.name, to, res.ok))
            log(d.action.value, channel.name, to, template_id, res.mode,
                "1" if res.ok else "0", body[:160].replace("\n", " "))
            if tripped:
                self._alert_breaker(channel.name, report, now,
                                    f"{self.cfg.breaker['consecutive_send_failures_to_pause']} consecutive send failures")
            return res.ok

        # ---------------- One branch per action ----------------
        if d.action in (ActionType.SKIP, ActionType.HOLD):
            if not inv.is_open():
                updates["agent_state"] = AgentState.CLOSED.value
            log(d.action.value, preview="; ".join(d.reasons)[:160])

        elif d.action is ActionType.ESCALATE:
            # High risk: notify the team on Slack and by email, **send nothing to the customer**
            deliver("slack", "SLACK_HIGH_ALERT", cust.am_slack_id or "#ar-alerts")
            d_action_backup = d.action
            d.action = ActionType.GMAIL_FINANCE_ALERT
            deliver("gmail_internal", "MAIL_FINANCE_ALERT", self.cfg.company["finance_lead_email"])
            d.action = d_action_backup
            updates["agent_state"] = AgentState.ESCALATED.value
            updates["agent_owner"] = "finance_lead"
            updates["agent_note"] = "; ".join(d.policy_hits or d.reasons)[:200]
            updates["agent_last_action_at"] = now.isoformat(timespec="seconds")
            report.escalations.append(inv.invoice_id)

        elif d.action is ActionType.SLACK_AM_NOTICE:
            ok = deliver("slack", "SLACK_AM_BUFFER", cust.am_slack_id or "#ar-collections")
            if ok:
                updates["agent_state"] = AgentState.AWAITING_AM.value
                updates["agent_buffer_until"] = d.detail["buffer_until"]
                updates["agent_owner"] = cust.account_manager
                updates["agent_last_action_at"] = now.isoformat(timespec="seconds")

        elif d.action is ActionType.WHATSAPP_REMINDER:
            template_id = d.detail["template_id"]
            if deliver("whatsapp", template_id, cust.whatsapp):
                updates["agent_stage"] = inv.agent_stage + 1
                updates["agent_state"] = AgentState.AUTO.value
                updates["agent_last_action_at"] = now.isoformat(timespec="seconds")

        elif d.action is ActionType.GMAIL_DUNNING:
            if deliver("gmail", "MAIL_MID_DUNNING", cust.email):
                updates["agent_stage"] = inv.agent_stage + 1
                updates["agent_state"] = AgentState.AUTO.value
                updates["agent_last_action_at"] = now.isoformat(timespec="seconds")
                updates["agent_note"] = "account manager did not respond within the buffer; the agent stepped in"

        self.store.update_agent_fields(inv.invoice_id, **updates)

    def _alert_breaker(self, channel: str, report: ScanReport, now: datetime, reason: str) -> None:
        subject, body = tpl.render("SLACK_BREAKER", {
            "reason": reason, "timestamp": now.isoformat(timespec="seconds"), "channel": channel,
        })
        msg = Message("slack", "#ar-alerts", subject, body, "SLACK_BREAKER", "", "", "internal")
        self.channels["slack"].send(msg)
        self.audit.write(
            timestamp=now.isoformat(timespec="seconds"), run_id=report.run_id,
            actor=Actor.SYSTEM.value, action="breaker_tripped", channel=channel,
            template_id="SLACK_BREAKER", reasons=[reason],
        )

    # ------------------------------------------------------------------ #
    # Main entry point
    # ------------------------------------------------------------------ #
    def daily_scan(self, now: datetime | None = None, dry_plan: bool = False) -> ScanReport:
        now = now or datetime.now()
        report = ScanReport(run_id=uuid.uuid4().hex[:8], run_at=now)

        # A dry plan must have no side effects at all: the matrix command and the dashboard both use this path,
        # and just looking should never mark inbox messages as processed.
        if not dry_plan:
            report.inbound_processed = self.process_inbox(upto=now)
        intents = self._pending_intents(upto=now)

        customers = self.store.load_customers()
        invoices = [i for i in self.store.load_invoices()]

        # L6 breaker 1: batch cap per scan
        batch, deferred = self.breaker.batch(invoices)
        report.deferred = len(deferred)
        if deferred and not dry_plan:
            self.audit.write(
                timestamp=now.isoformat(timespec="seconds"), run_id=report.run_id,
                actor=Actor.SYSTEM.value, action="batch_capped",
                reasons=[f"this scan: {len(batch)} invoices; {len(deferred)} deferred to the next batch "
                         f"(cap {self.cfg.breaker['max_invoices_per_scan']})"],
            )

        sends_today = self.breaker.sends_on(now.date())

        for inv in batch:
            cust = customers.get(inv.customer_id)
            if cust is None:
                if dry_plan:
                    continue
                self.audit.write(
                    timestamp=now.isoformat(timespec="seconds"), run_id=report.run_id,
                    actor=Actor.SYSTEM.value, invoice_id=inv.invoice_id, action="skip",
                    reasons=[f"customer master data missing: {inv.customer_id}"],
                )
                continue

            report.scanned += 1
            d = self.decide(inv, cust, now, intents, sends_today)
            report.count(d.tier)
            report.actions.append(d)

            if d.detail.get("halt") and not report.halted:
                report.halted = True
                report.halt_reason = f"daily customer send cap of {self.cfg.policy['daily_send_cap']} reached"
                if not dry_plan:
                    self._alert_breaker("all", report, now, report.halt_reason)

            if dry_plan:
                continue

            self.execute(d, report, now)
            sends_today = self.breaker.sends_on(now.date())

        if not dry_plan:
            self.breaker.save()
        return report

    # ------------------------------------------------------------------ #
    # Human takeover
    # ------------------------------------------------------------------ #
    def takeover(self, invoice_id: str, by: str, note: str = "") -> None:
        """An account manager or finance explicitly takes over an invoice; the agent stops all actions on it at once.

        Deliberately an explicit command rather than "replying in Slack counts as a takeover":
        a takeover changes who is authorised to act, so it needs a clear, auditable action.
        """
        now = datetime.now()
        self.store.update_agent_fields(
            invoice_id,
            agent_state=AgentState.HUMAN_TAKEN.value,
            agent_owner=by,
            agent_last_action_at=now.isoformat(timespec="seconds"),
            agent_note=note or f"taken over by {by} at {now:%Y-%m-%d %H:%M}",
        )
        self.audit.write(
            timestamp=now.isoformat(timespec="seconds"), run_id="manual",
            actor=Actor.HUMAN.value, invoice_id=invoice_id, action="takeover",
            reasons=[f"taken over by {by}; the agent stops automatic follow-up"], content_preview=note,
        )

    def release(self, invoice_id: str, by: str) -> None:
        """A person hands the invoice back to the agent for follow-up."""
        now = datetime.now()
        self.store.update_agent_fields(
            invoice_id,
            agent_state=AgentState.NEW.value, agent_owner="", agent_buffer_until="",
            agent_note=f"handed back to the agent by {by} at {now:%Y-%m-%d %H:%M}",
        )
        self.audit.write(
            timestamp=now.isoformat(timespec="seconds"), run_id="manual",
            actor=Actor.HUMAN.value, invoice_id=invoice_id, action="release",
            reasons=[f"{by} handed the invoice back to the agent"],
        )

    def mark_paid(self, invoice_id: str, on: date | None = None) -> None:
        """Mark an invoice as paid.

        Note: this method is **not an agent capability**. It writes business fields such as status/paid_date,
        and the agent's service account has no write permission on them (see the allow-list in store/base.py).
        This is a direct table edit under a finance user's identity, and the CLI labels it as a human action.
        """
        rows = self.store._read_raw()
        on = on or date.today()
        for row in rows:
            if row["invoice_id"].strip() == invoice_id:
                row["status"] = "paid"
                row["paid_date"] = on.isoformat()
                row["agent_state"] = AgentState.CLOSED.value
                break
        else:
            raise KeyError(f"invoice not found: {invoice_id}")
        self.store._write_raw(rows)
        self.audit.write(
            run_id="manual", actor=Actor.HUMAN.value, invoice_id=invoice_id,
            action="mark_paid", reasons=[f"finance confirmed payment received {on.isoformat()}"],
        )
