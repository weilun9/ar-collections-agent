"""Guardrails and routing, tested through the real scan pipeline.

test_ar_agent.py covers each node (tiering, policy, templates, store, breaker) mostly in isolation. This file covers
what happens when those nodes are wired together, especially the paths that protect customers:

    * who may be contacted, and when (disputes, "already paid", VIPs, daily cap, takeover, settled invoices);
    * escalation to a person, and that it happens once rather than on every scan;
    * failure handling: a dead channel, a message blocked by the content gate, missing master data;
    * the routing state machine (light cadence, release back to the agent, time-travel replies);
    * the live/dry-run gate and the live payloads each channel would send (network mocked).

Standard-library unittest; also collected by pytest. No network, no model, no API key.
"""
from __future__ import annotations

import copy
import csv
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ar_agent.breaker import CircuitBreaker                      # noqa: E402
from ar_agent.channels.gmail import GmailCustomerChannel         # noqa: E402
from ar_agent.channels.slack import SlackChannel                 # noqa: E402
from ar_agent.channels.whatsapp import WhatsAppChannel           # noqa: E402
from ar_agent.config import Config                               # noqa: E402
from ar_agent.inbound import ReplyIntent, classify_reply         # noqa: E402
from ar_agent.models import ActionType, Actor, AgentState, Message, SendResult, Tier  # noqa: E402
from ar_agent.orchestrator import Orchestrator                   # noqa: E402
from ar_agent.seed import seed as seed_data                      # noqa: E402
from ar_agent.templates import TemplateViolation, _guard         # noqa: E402

T0 = datetime(2026, 8, 25, 9)
ANCHOR = date(2026, 8, 25)
NO_LLM = {"AR_LLM_PROVIDER": "", "AR_LLM_MODEL": "", "AR_LIVE": ""}


def cfg_with(**overrides) -> Config:
    """The shipped config with some dotted keys overridden, e.g. cfg_with(**{"policy.daily_send_cap": 2})."""
    raw = copy.deepcopy(Config.load(ROOT / "config.json").raw)
    for dotted, value in overrides.items():
        node = raw
        *parents, leaf = dotted.split(".")
        for p in parents:
            node = node[p]
        node[leaf] = value
    return Config(raw)


@mock.patch.dict(os.environ, NO_LLM)
class ScanCase(unittest.TestCase):
    """Base class: a scratch project root seeded with the 30 demo invoices (anchor 2026-08-25)."""

    cfg_overrides: dict = {}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        shutil.copy(ROOT / "config.json", self.tmp / "config.json")
        seed_data(self.tmp / "data", ANCHOR)
        self.orc = Orchestrator(self.tmp, cfg_with(**self.cfg_overrides))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers ----
    def outbox(self, audience=None, invoice_id=None, template_id=None) -> list[dict]:
        out = []
        for f in sorted((self.tmp / "data" / "outbox").glob("*.json")):
            m = json.loads(f.read_text(encoding="utf-8"))
            if audience and m["audience"] != audience:
                continue
            if invoice_id and m["invoice_id"] != invoice_id:
                continue
            if template_id and m["template_id"] != template_id:
                continue
            out.append(m)
        return out

    def decision(self, report, invoice_id):
        return next(d for d in report.actions if d.invoice.invoice_id == invoice_id)

    def invoice(self, invoice_id):
        return next(i for i in self.orc.store.load_invoices() if i.invoice_id == invoice_id)

    def add_reply(self, invoice_id, customer_id, text, at: datetime):
        rows = self.orc._read_inbox()
        rows.append({"reply_id": f"R-T{len(rows)}", "invoice_id": invoice_id, "customer_id": customer_id,
                     "received_at": at.isoformat(timespec="seconds"), "channel": "whatsapp",
                     "text": text, "processed": "", "intent": ""})
        self.orc._write_inbox(rows)

    def set_contact_name(self, customer_id, name):
        path = self.tmp / "data" / "customers.csv"
        with open(path, encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        for r in rows:
            if r["customer_id"] == customer_id:
                r["contact_name"] = name
        with open(path, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)


# --------------------------------------------------------------------------- #
# Who may be contacted
# --------------------------------------------------------------------------- #

class TestContactRules(ScanCase):

    def test_dispute_goes_to_a_person_and_customer_is_never_contacted(self):
        """INV-2025: the customer says the amount is wrong and mentions lawyers (P-02)."""
        rep = self.orc.daily_scan(now=T0)
        d = self.decision(rep, "INV-2025")
        self.assertIs(d.base_tier, Tier.MID, "on facts alone it is medium; the dispute is what makes it high")
        self.assertIs(d.tier, Tier.HIGH)
        self.assertIs(d.action, ActionType.ESCALATE)
        self.assertTrue(any(h.startswith("P-02") for h in d.policy_hits))
        self.assertEqual(self.outbox("customer", "INV-2025"), [])
        self.assertEqual(len(self.outbox("internal", "INV-2025")), 2, "one Slack alert + one email to the finance lead")
        self.assertEqual(self.invoice("INV-2025").agent_state, AgentState.ESCALATED.value)

    def test_already_paid_claim_pauses_collection_without_escalating(self):
        """INV-2024: the customer says it has paid (P-06). Chasing a customer who paid is the worst avoidable mistake."""
        rep = self.orc.daily_scan(now=T0)
        d = self.decision(rep, "INV-2024")
        self.assertIs(d.action, ActionType.HOLD)
        self.assertIsNot(d.tier, Tier.HIGH, "saying it has paid is not a risk signal")
        self.assertFalse(d.agent_may_contact_customer)
        # ... and it stays paused on later scans until finance acts
        for days in (1, 7, 14):
            self.orc.daily_scan(now=T0 + timedelta(days=days))
        self.assertEqual(self.outbox("customer", "INV-2024"), [])

    def test_vip_gets_internal_alert_only(self):
        rep = self.orc.daily_scan(now=T0)
        d = self.decision(rep, "INV-2021")          # VIP, would be light on facts alone
        self.assertIs(d.base_tier, Tier.LOW)
        self.assertIs(d.action, ActionType.ESCALATE)
        self.assertEqual(self.outbox("customer", "INV-2021"), [])
        self.assertTrue(self.outbox("internal", "INV-2021"))

    def test_settled_invoice_is_closed_and_never_contacted(self):
        self.orc.mark_paid("INV-2001", on=ANCHOR)
        rep = self.orc.daily_scan(now=T0)
        self.assertIs(self.decision(rep, "INV-2001").action, ActionType.SKIP)
        self.assertEqual(self.invoice("INV-2001").agent_state, AgentState.CLOSED.value)
        self.assertEqual(self.outbox(invoice_id="INV-2001"), [])

    def test_customer_messages_only_use_reviewed_customer_templates(self):
        """Invariant over a multi-day run: every customer-facing message used a customer template and passes the gate."""
        from ar_agent.templates import TEMPLATES
        for days in (0, 2, 7, 14):
            self.orc.daily_scan(now=T0 + timedelta(days=days))
        msgs = self.outbox("customer")
        self.assertTrue(msgs)
        for m in msgs:
            with self.subTest(template=m["template_id"], invoice=m["invoice_id"]):
                self.assertEqual(TEMPLATES[m["template_id"]].audience, "customer")
                _guard(m["body"], m["template_id"])        # raises on forbidden wording


class TestEscalation(ScanCase):

    def test_escalation_happens_once_not_on_every_scan(self):
        self.orc.daily_scan(now=T0)
        alerts_before = len(self.outbox("internal", "INV-2019"))
        rep = self.orc.daily_scan(now=T0 + timedelta(days=1))
        d = self.decision(rep, "INV-2019")
        self.assertIs(d.action, ActionType.HOLD, "an escalated invoice belongs to a person now")
        self.assertEqual(len(self.outbox("internal", "INV-2019")), alerts_before, "no repeat alerts")

    def test_high_risk_alert_states_nothing_was_sent_and_why(self):
        self.orc.daily_scan(now=T0)
        slack = self.outbox("internal", "INV-2023", "SLACK_HIGH_ALERT")[0]
        self.assertIn("The system has sent nothing to this customer", slack["body"])
        self.assertIn("P-01", slack["body"], "the rule that fired must be named in the alert")
        mail = self.outbox("internal", "INV-2023", "MAIL_FINANCE_ALERT")[0]
        self.assertEqual(mail["to"], self.orc.cfg.company["finance_lead_email"])

    def test_late_instalment_request_stops_a_running_cadence(self):
        """A light invoice already being chased gets an instalment request: no more customer messages, ever."""
        self.orc.daily_scan(now=T0)
        self.assertEqual(len(self.outbox("customer", "INV-2003")), 1)
        self.add_reply("INV-2003", "C007", "Can we extend the payment by a month?", T0 + timedelta(days=2))
        for days in (3, 7, 10):
            self.orc.daily_scan(now=T0 + timedelta(days=days))
        self.assertEqual(len(self.outbox("customer", "INV-2003")), 1)
        self.assertEqual(self.invoice("INV-2003").agent_state, AgentState.ESCALATED.value)


class TestHumanInTheLoop(ScanCase):

    def test_takeover_then_release_hands_control_back(self):
        self.orc.takeover("INV-2001", by="Wang")
        rep = self.orc.daily_scan(now=T0)
        self.assertIs(self.decision(rep, "INV-2001").action, ActionType.HOLD)
        self.assertEqual(self.outbox(invoice_id="INV-2001"), [])

        self.orc.release("INV-2001", by="Wang")
        rep = self.orc.daily_scan(now=T0 + timedelta(hours=1))
        self.assertIs(self.decision(rep, "INV-2001").action, ActionType.WHATSAPP_REMINDER)

    def test_takeover_during_mid_buffer_prevents_the_dunning_email(self):
        self.orc.daily_scan(now=T0)                                     # AM notified, buffer starts
        self.orc.takeover("INV-2010", by="王磊")
        rep = self.orc.daily_scan(now=T0 + timedelta(hours=49))         # buffer would have expired
        self.assertIs(self.decision(rep, "INV-2010").action, ActionType.HOLD)
        self.assertEqual(self.outbox("customer", "INV-2010"), [])

    def test_audit_log_records_who_acted(self):
        self.orc.daily_scan(now=T0)
        self.orc.takeover("INV-2010", by="王磊", note="calling the customer")
        log = self.orc.audit.read_all()
        takeover = [r for r in log if r["action"] == "takeover"]
        self.assertEqual([r["actor"] for r in takeover], [Actor.HUMAN.value])
        sends = [r for r in log if r["action"] == ActionType.WHATSAPP_REMINDER.value]
        self.assertTrue(sends and all(r["actor"] == Actor.AGENT.value for r in sends))
        self.assertTrue(all(r["mode"] == "dry_run" for r in sends))
        esc = next(r for r in log if r["invoice_id"] == "INV-2023" and r["channel"] == "slack")
        self.assertIn("P-01", esc["policy_hits"], "the audit row must say which rule fired")
        self.assertEqual(esc["base_tier"], "low")
        self.assertEqual(esc["tier"], "high")


# --------------------------------------------------------------------------- #
# Routing / state machine
# --------------------------------------------------------------------------- #

class TestRouting(ScanCase):

    def test_first_scan_routes_each_tier_to_its_action(self):
        rep = self.orc.daily_scan(now=T0)
        expect = {
            "INV-2001": ActionType.WHATSAPP_REMINDER,   # light
            "INV-2010": ActionType.SLACK_AM_NOTICE,     # medium (amount)
            "INV-2007": ActionType.SLACK_AM_NOTICE,     # medium (history)
            "INV-2016": ActionType.ESCALATE,            # high (days)
            "INV-2018": ActionType.ESCALATE,            # high (amount)
            "INV-2026": ActionType.SKIP,                # not yet due
            "INV-2028": ActionType.SKIP,                # paid
        }
        for inv_id, action in expect.items():
            with self.subTest(invoice=inv_id):
                self.assertIs(self.decision(rep, inv_id).action, action)

    def test_light_tier_stops_after_max_reminders(self):
        self.orc.daily_scan(now=T0)                                   # INV-2004: reminder 1 (1 day overdue)
        self.orc.daily_scan(now=T0 + timedelta(days=7))               # reminder 2
        rep = self.orc.daily_scan(now=T0 + timedelta(days=13))        # 14 days overdue: still light
        d = self.decision(rep, "INV-2004")
        self.assertIs(d.tier, Tier.LOW)
        self.assertIs(d.action, ActionType.HOLD)
        self.assertEqual(len(self.outbox("customer", "INV-2004")), 2)

    def test_mid_tier_formal_email_is_sent_once_then_escalates_on_age(self):
        """Regression: after the dunning email the AM was re-notified and another formal email followed every 3 days."""
        for day in range(0, 30):                                      # INV-2007: 5 days overdue at T0 -> 31 on day 26
            self.orc.daily_scan(now=T0 + timedelta(days=day))
        self.assertEqual(len(self.outbox("customer", "INV-2007", "MAIL_MID_DUNNING")), 1)
        self.assertEqual(len(self.outbox("internal", "INV-2007", "SLACK_AM_BUFFER")), 1, "the AM is notified once")
        self.assertEqual(self.invoice("INV-2007").agent_state, AgentState.ESCALATED.value,
                         "once it is over 30 days it goes to a person")

    def test_light_invoice_that_ages_into_mid_goes_to_the_account_manager(self):
        self.orc.daily_scan(now=T0)                                   # INV-2006: 14 days, light reminder
        rep = self.orc.daily_scan(now=T0 + timedelta(days=1))         # 15 days -> medium
        d = self.decision(rep, "INV-2006")
        self.assertIs(d.tier, Tier.MID)
        self.assertIs(d.action, ActionType.SLACK_AM_NOTICE)

    def test_replies_from_the_future_are_ignored_when_replaying_history(self):
        self.add_reply("INV-2001", "C001", "能不能分三期付？", T0 + timedelta(days=5))
        rep = self.orc.daily_scan(now=T0)
        self.assertIs(self.decision(rep, "INV-2001").tier, Tier.LOW, "a scan must not see a reply received later")
        rep = self.orc.daily_scan(now=T0 + timedelta(days=6))
        self.assertIs(self.decision(rep, "INV-2001").tier, Tier.HIGH)

    def test_highest_risk_reply_wins_regardless_of_order(self):
        for first, second in (("收到，本周安排付款", "能不能分期"), ("能不能分期", "收到，本周安排付款")):
            with self.subTest(order=(first, second)):
                inbox = self.orc.inbox_path
                inbox.unlink()
                self.add_reply("INV-2001", "C001", first, T0 - timedelta(hours=2))
                self.add_reply("INV-2001", "C001", second, T0 - timedelta(hours=1))
                intent, _ = self.orc._pending_intents(upto=T0)["INV-2001"]
                self.assertEqual(intent, ReplyIntent.TERMS_CHANGE.value)

    def test_ack_reply_does_not_escalate(self):
        rep = self.orc.daily_scan(now=T0)
        d = self.decision(rep, "INV-2002")                            # customer replied "收到，本周内会安排付款"
        self.assertIs(d.tier, Tier.LOW)
        self.assertIs(d.action, ActionType.WHATSAPP_REMINDER)
        self.assertEqual(d.policy_hits, [])

    def test_english_replies_are_classified_too(self):
        cases = {
            "Could we have a grace period?": ReplyIntent.TERMS_CHANGE,
            "We dispute this invoice.": ReplyIntent.DISPUTE,
            "Payment sent yesterday.": ReplyIntent.PAID_CLAIM,
            "Noted, will pay Friday.": ReplyIntent.ACK,
        }
        for text, intent in cases.items():
            with self.subTest(text=text):
                self.assertIs(classify_reply(text).intent, intent)


# --------------------------------------------------------------------------- #
# Failure handling
# --------------------------------------------------------------------------- #

class TestDailyCap(ScanCase):
    cfg_overrides = {"policy.daily_send_cap": 2}

    def test_cap_halts_customer_sends_but_escalations_still_reach_people(self):
        rep = self.orc.daily_scan(now=T0)
        self.assertTrue(rep.halted)
        self.assertEqual(len(self.outbox("customer")), 2, "exactly the cap, not one more")
        self.assertTrue(self.outbox("internal", template_id="SLACK_BREAKER"), "finance is told sending paused")
        # high-risk invoices processed after the halt are still escalated to a person
        self.assertTrue(self.outbox("internal", "INV-2019", "SLACK_HIGH_ALERT"))

    def test_internal_alerts_do_not_count_toward_the_customer_cap(self):
        b = CircuitBreaker(self.tmp / "b.json", self.orc.cfg)
        for _ in range(5):
            b.record_send("slack", True, ANCHOR, customer_facing=False)
        self.assertEqual(b.sends_on(ANCHOR), 0)
        b.record_send("whatsapp", True, ANCHOR)
        self.assertEqual(b.sends_on(ANCHOR), 1)


class TestChannelFailures(ScanCase):

    def test_dead_customer_channel_trips_breaker_and_state_is_not_advanced(self):
        wa = self.orc.channels["whatsapp"]
        wa.live = True                                                # pretend live, with a dead API
        with mock.patch.object(WhatsAppChannel, "_send_live", side_effect=ConnectionError("API down")):
            self.orc.daily_scan(now=T0)
        n = self.orc.cfg.breaker["consecutive_send_failures_to_pause"]
        self.assertTrue(self.orc.breaker.is_tripped("whatsapp"))
        log = [r for r in self.orc.audit.read_all() if r["channel"] == "whatsapp"]
        self.assertEqual(sum(r["ok"] == "0" and r["mode"] == "live" for r in log), n, "stops trying after the threshold")
        self.assertTrue(any(r["mode"] == "blocked" for r in log), "later sends are skipped, not retried")
        self.assertTrue(self.outbox("internal", template_id="SLACK_BREAKER"))
        for inv_id in ("INV-2001", "INV-2002", "INV-2003"):
            self.assertEqual(self.invoice(inv_id).agent_stage, 0, "a failed send must not count as a reminder")
        # persisted: the next process sees the tripped breaker
        self.assertTrue(CircuitBreaker(self.tmp / "data" / "breaker_state.json", self.orc.cfg).is_tripped("whatsapp"))

    def test_failed_am_notice_is_retried_next_scan(self):
        real_send_dry = SlackChannel._send_dry

        def flaky(channel, msg):          # only the notice for INV-2010 fails; everything else goes through
            if msg.template_id == "SLACK_AM_BUFFER" and msg.invoice_id == "INV-2010":
                return SendResult(False, "slack", msg.to, "dry_run", "simulated failure")
            return real_send_dry(channel, msg)

        with mock.patch.object(SlackChannel, "_send_dry", flaky):
            self.orc.daily_scan(now=T0)
        self.assertNotEqual(self.invoice("INV-2010").agent_state, AgentState.AWAITING_AM.value,
                            "the buffer must not start if the account manager was never told")
        rep = self.orc.daily_scan(now=T0 + timedelta(hours=1))
        self.assertIs(self.decision(rep, "INV-2010").action, ActionType.SLACK_AM_NOTICE)
        self.assertEqual(self.invoice("INV-2010").agent_state, AgentState.AWAITING_AM.value)

    def test_content_gate_blocks_one_message_without_stopping_the_scan(self):
        """Regression: a guarded word in a contact name (here 立即, 'immediately') used to abort the whole scan."""
        self.set_contact_name("C001", "张伟（立即回电）")
        rep = self.orc.daily_scan(now=T0)
        self.assertIn("INV-2001", [b[0] for b in rep.blocked])
        self.assertEqual(self.outbox("customer", "INV-2001"), [], "the offending message is not sent")
        self.assertEqual(self.invoice("INV-2001").agent_stage, 0)
        self.assertTrue(self.outbox("customer", "INV-2003"), "other invoices are still handled")
        self.assertEqual(self.orc.breaker.state.consecutive_failures.get("whatsapp", 0), 0,
                         "a content problem is not a channel failure")
        row = next(r for r in self.orc.audit.read_all() if r["invoice_id"] == "INV-2001" and r["mode"] == "blocked")
        self.assertIn("content gate", row["content_preview"])

    def test_guard_is_case_insensitive(self):
        """Regression: 'INSTALLMENT' or 'Discount' must be caught like their lower-case forms."""
        for text in ("We accept an INSTALLMENT plan", "A special Discount for you", "We can WAIVE the fee"):
            with self.subTest(text=text):
                with self.assertRaises(TemplateViolation):
                    _guard(text, "T")

    def test_missing_customer_master_data_is_skipped_and_logged(self):
        rows = self.orc.store._read_raw()
        orphan = dict(rows[0], invoice_id="INV-9001", customer_id="C999")
        self.orc.store._write_raw(rows + [orphan])
        rep = self.orc.daily_scan(now=T0)
        self.assertNotIn("INV-9001", [d.invoice.invoice_id for d in rep.actions])
        row = next(r for r in self.orc.audit.read_all() if r["invoice_id"] == "INV-9001")
        self.assertEqual(row["actor"], Actor.SYSTEM.value)
        self.assertIn("customer master data missing", row["reasons"])
        self.assertEqual(self.outbox(invoice_id="INV-9001"), [])


class TestBatchCap(ScanCase):
    cfg_overrides = {"breaker.max_invoices_per_scan": 10}

    def test_batch_cap_defers_the_rest_and_logs_it(self):
        rep = self.orc.daily_scan(now=T0)
        self.assertEqual((rep.scanned, rep.deferred), (10, 20))
        self.assertTrue(any(r["action"] == "batch_capped" for r in self.orc.audit.read_all()))


# --------------------------------------------------------------------------- #
# Live gate and live payloads (network mocked)
# --------------------------------------------------------------------------- #

class TestLiveGate(unittest.TestCase):

    def test_live_needs_both_config_and_env(self):
        cases = [(False, "0", False), (False, "1", False), (True, "0", False), (True, "", False), (True, "1", True)]
        for configured, env, expected in cases:
            with self.subTest(config_live=configured, AR_LIVE=env):
                cfg = cfg_with(**{"channels.whatsapp.live": configured})
                with mock.patch.dict(os.environ, {"AR_LIVE": env}):
                    self.assertIs(cfg.channel_is_live("whatsapp"), expected)


class _FakeResponse:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestLivePayloads(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def msg(self, **kw):
        base = dict(channel="whatsapp", to="+8613800000101", subject="", body="rendered text",
                    template_id="WA_LOW_1", invoice_id="INV-2001", customer_id="C001", audience="customer",
                    variables={"contact_name": "张伟", "company": "A", "invoice_id": "INV-2001",
                               "amount": "12,000.00", "due_date": "2026-08-22", "payment_url": "https://x/"})
        base.update(kw)
        return Message(**base)

    @mock.patch.dict(os.environ, {"WHATSAPP_PHONE_NUMBER_ID": "123", "WHATSAPP_TOKEN": "tok"})
    def test_whatsapp_live_sends_an_approved_template_not_free_text(self):
        sent = []

        def fake_urlopen(req, timeout):
            sent.append(json.loads(req.data))
            return _FakeResponse({"messages": [{"id": "wamid.1"}]})

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            res = WhatsAppChannel(True, self.tmp).send(self.msg())
        self.assertTrue(res.ok)
        self.assertEqual(res.ref, "wamid.1")
        self.assertEqual(sent[0]["type"], "template")
        self.assertNotIn("rendered text", json.dumps(sent[0], ensure_ascii=False), "the free-text body is never sent")

    @mock.patch.dict(os.environ, {"WHATSAPP_PHONE_NUMBER_ID": "123", "WHATSAPP_TOKEN": "tok"})
    def test_whatsapp_live_error_is_a_failed_send_not_a_crash(self):
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("slow")):
            res = WhatsAppChannel(True, self.tmp).send(self.msg())
        self.assertFalse(res.ok)
        self.assertIn("TimeoutError", res.detail)

    @mock.patch.dict(os.environ, {"SLACK_BOT_TOKEN": "xoxb-test"})
    def test_slack_api_error_is_reported_as_failure(self):
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse({"ok": False, "error": "channel_not_found"})):
            res = SlackChannel(True, self.tmp).send(self.msg(channel="slack", audience="internal", to="#x"))
        self.assertFalse(res.ok)
        self.assertEqual(res.detail, "channel_not_found")

    @mock.patch.dict(os.environ, {"GMAIL_USER": "ar@example.com", "GMAIL_APP_PASSWORD": "pw"})
    def test_gmail_live_uses_starttls_and_sends_rendered_body(self):
        smtp = mock.MagicMock()
        with mock.patch("smtplib.SMTP", return_value=smtp):
            res = GmailCustomerChannel(True, self.tmp).send(
                self.msg(channel="gmail", to="zhang@hengxin.example", subject="付款提醒", template_id="MAIL_MID_DUNNING"))
        self.assertTrue(res.ok)
        conn = smtp.__enter__.return_value
        conn.starttls.assert_called_once()
        mail = conn.send_message.call_args[0][0]
        self.assertEqual(mail["To"], "zhang@hengxin.example")
        self.assertIn("rendered text", mail.get_content())

    def test_audience_mismatch_is_blocked_before_any_live_call(self):
        with mock.patch.object(WhatsAppChannel, "_send_live") as live:
            res = WhatsAppChannel(True, self.tmp).send(self.msg(audience="internal"))
        live.assert_not_called()
        self.assertEqual(res.mode, "blocked")


if __name__ == "__main__":
    unittest.main(verbosity=2)
