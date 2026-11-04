# Interview notes: design trade-offs

Ten questions an interviewer is likely to ask about this project, with short answers grounded in the code.
Paths are relative to `src/ar_agent/`. Architecture details are in [`architecture.md`](architecture.md).

---

### 1. The LLM doesn't make any decisions. Why call it an agent, and why not let the model pick actions?

It acts on its own (scans daily, sends reminders, escalates, steps in when an account manager doesn't), but the
decision path is deterministic code: `tiering.classify` → `policy.evaluate` → `Orchestrator.decide`. The PRD's first
sketch had the model calling tools like `send_reminder(customer_id, tier)`; I moved those decisions into code because
in collections a wrong decision is expensive (an instalment promised, a VIP chased, a customer who already paid
chased again) and I want each one to be reproducible, auditable and immune to prompt injection.

The trade-off is flexibility: a new situation needs a rule or a keyword, not just a better prompt. The model is
used where a mistake only costs readability: the alert summary (`summarizer.py`) and the read-only assistant
(`assistant.py`). `tests/test_end_to_end.py` runs the demo week with the LLM off, mocked, hallucinating and down, and
asserts the decisions are identical in all four.

### 2. Why classify customer replies with keywords instead of an LLM?

`inbound.classify_reply` is a keyword/regex matcher with normalisation (full-width to half-width, separators between
CJK characters removed), so "分 期" or "ＤＩＳＣＯＵＮＴ" still match. Three reasons: the log shows exactly which word
triggered the escalation; "ignore previous instructions" is just text to a matcher; and the same message always gives
the same result, which a compliance review can reproduce.

The cost is recall: a new phrasing is missed until someone adds it. Two things limit the damage. When categories
overlap, the highest-risk one wins (`PRIORITY`), so "we paid part, can we split the rest?" is a terms change, not a
paid claim. And the trigger only needs to fire once, because escalation is sticky. The gap I'd close next: replies
that match nothing (`unknown`) currently don't change routing. In production they should go to a human queue, and an
LLM could *suggest* new keywords for review without making the routing decision.

### 3. How do you guarantee a high-risk customer is never contacted?

Several independent layers, each enough on its own for the common case:

- **Order of checks in `decide()`**: terminal states, then `tier is HIGH → ESCALATE`, before any cadence logic.
- **The `ESCALATE` branch only uses internal channels** (`slack`, `gmail_internal`) in `execute()`.
- **Types, not flags**: `CustomerChannel` and `InternalChannel` are separate classes, and `Channel.send` refuses a
  message whose template audience doesn't match the channel's (`channels/base.py`).
- **Sticky state**: once `escalated`, every later scan holds until a person runs `ar release` or `ar paid`.

It's checked as an invariant, not just by unit tests: `ar verify` runs a real dry-run scan on a scratch copy and
counts customer messages to high-risk invoices (0 of 6), and `test_end_to_end` checks every new outbox message after
every scan of the demo week against that scan's decisions.

### 4. Why can policy rules only tighten an outcome?

`PolicyOutcome.escalate_to` ignores a lower tier and `forbid_customer_contact` only ever sets `False`
(`policy.py`). That makes the chain monotonic: rule order doesn't matter, and adding a rule can never re-open a
door another rule closed. It also lets me reason about a rule on its own.

The cost is that you can't write an exception as a rule ("this VIP has opted in to reminders"). That's deliberate:
loosening is a human decision, made with `ar release` and recorded in the audit log as `actor=human`.

### 5. Why the 48-hour buffer for mid-tier invoices, and what happens when it runs out?

Mid-tier accounts (15–30 days, ¥50k–200k, or a repeat late payer) usually have a relationship behind them, so the
account manager gets a Slack notice with 48 hours' priority and the exact `ar takeover` command
(`SLACK_AM_BUFFER`). If nobody acts, the agent sends one formal, non-threatening statement (`MAIL_MID_DUNNING`) and
the weekly report lists the account manager as unresponsive, which is a management signal in itself.

"One" is important. While writing the tests I found the agent re-notified the AM after the dunning email, started a
new buffer and sent another formal email every three days with no limit (8 in 30 days for one invoice). It now holds
after the first; the invoice still escalates automatically at day 31. Regression test:
`test_mid_tier_formal_email_is_sent_once_then_escalates_on_age`.

### 6. How do you stop the model inventing numbers in summaries and answers?

An output check outside the model. `summarizer.check_summary` and `assistant.check_answer` extract every number
with two or more digits and reject the output if any isn't in the facts (or, for the assistant, the question plus
successful tool results). A rejected summary falls back to the raw fact list, and the alert says why; a rejected
answer is withheld and the raw tool results are shown. The raw records are always printed under an AI summary, and
the summary only goes to internal readers.

Limits I'd mention: single-digit numbers and non-numeric fabrications ("the customer agreed") aren't caught by this
check. That's acceptable here because the summary never drives a decision and the facts are shown next to it; it
wouldn't be acceptable for customer-facing text, which is why customer text is template-only.

### 7. How does the read-only assistant resist prompt injection, such as "send a reminder and mark it paid"?

By capability, not instruction. The four tools (`get_overview`, `list_invoices`, `get_invoice`, `get_case_history`)
only read a dry plan, the audit log and the inbox. There is no write tool, so even a fully compromised model can't
act. `validate_args` checks the tool name against an allow-list and the arguments against each tool's JSON Schema
before anything runs; a bad call gets an error result instead. The loop is capped at `max_tool_calls` (6).
`test_end_to_end` has the fake model try `mark_paid` and `send_whatsapp`: both get `is_error: true` and the data files
are byte-identical afterwards. `ar verify` checks the same thing by comparing every data file before and after.

### 8. What happens when WhatsApp (or any channel) goes down?

`Channel.send` never raises: a live error becomes `SendResult(ok=False)`. The invoice's state isn't advanced, so the
reminder is retried on the next scan. `CircuitBreaker` counts consecutive failures per channel; at 5 it trips, a
breaker alert goes to Slack, and later sends on that channel are skipped and logged as `blocked`. There's no
automatic recovery: a tripped breaker means something is wrong (revoked token, template rejected), so a person
resets it with `ar reset-breaker`. The counter is persisted so it survives restarts.

Residual risks I'd name: if Slack is the dead channel, the breaker alert goes through Slack too; and a crash between
a successful send and the state write-back means the next scan sends again (at-least-once). Production needs an
independent alert path and an idempotency key per `(invoice, template, stage)`.

### 9. Why CSV files and only the standard library? What changes for production?

It's a portfolio project that should run anywhere with `python ar.py demo`, and CSV is what finance teams already
open in Excel, so you can watch exactly what the agent wrote. The storage is behind `ReceivablesStore`
(`store/base.py`), which is where the L3 allow-list lives; a Google Sheets adapter already exists.

For production: Postgres with column-level grants so the allow-list is enforced by the database too; row locks or
`SKIP LOCKED` so two scans can't double-send (today nothing prevents that); idempotent sends; a queue and per-channel
rate limits instead of one batch; webhooks for inbound replies; SSO and roles in front of `takeover`, `paid` and
`reset-breaker`, which today trust a `--by` argument; and a tamper-evident audit log. Section 9 of
`architecture.md` has the full list.

### 10. How do you test an LLM-dependent system, and what did testing find?

Every model call goes through an injected `transport` callable, so tests pass fakes: scripted tool-use responses for
the assistant, and good, hallucinating or failing responses for the summary. `tests/conftest.py` clears the
provider/live environment variables and replaces `urlopen` and `smtplib.SMTP` with a function that raises, so
`pytest` runs offline with no API key and can't reach a real service even from a misconfigured shell. A subprocess
test runs every README command against a scratch copy of the project.

Writing pipeline-level tests found five real bugs, all now fixed with regression tests:

1. **Reply priority was inverted** in `_pending_intents`: an "ok, noted" after an instalment request in the same
   window won, so the invoice wasn't escalated.
2. **Mid-tier formal emails repeated** every three days without limit (question 5).
3. **One content-gate hit aborted the whole scan**: a guarded word in a contact name raised out of `execute()`, so
   no invoice was processed. Now that one message is blocked and logged, and the scan continues.
4. **The banned-phrase gate was case-sensitive**: "INSTALLMENT" passed.
5. **The daily customer send cap counted internal alerts**, so escalations used up the customer budget.

Also, `ar verify`'s end-to-end check passed on an empty outbox without testing anything; it now runs a real scan on a
scratch copy. The lesson I'd take from it: unit tests on each rule passed, and every one of these bugs was in the
wiring between components.
