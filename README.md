# AR Collections Agent

An accounts-receivable collections agent for a B2B trading company: it scans overdue invoices every day,
sorts them into risk tiers, sends reminders through WhatsApp / Gmail, alerts the team on Slack, and writes a
weekly report for the CFO. **It can never promise a customer a discount, an instalment plan or an extension.**
Those decisions always go to a person.

The point of the project is the safety design. Customer-facing automation in finance fails in expensive ways,
so every rule that must not break runs as plain, tested Python **outside the model**. The LLM is used in two
places, and neither can change anything: an alert summary, and a read-only assistant whose tools cannot write.

- **Pure Python standard library** (zero third-party dependencies for the core), ~5k lines, **74 unit tests**
- A 13-act CLI demo, a local web dashboard, a CFO weekly report, and a `verify` command that checks every
  safety layer live (24/24 checks)
- Fully **dry-run by default**: messages are written to `data/outbox/`, never sent to anyone
- Product spec with problem statement, user stories, non-goals and guardrail metrics:
  [`docs/PRD.md`](docs/PRD.md) (Chinese original: [`docs/PRD.zh-CN.md`](docs/PRD.zh-CN.md))

> Code, CLI output, dashboard and internal alerts are in English. The demo data, customer-facing message templates and
> reply keywords are in Chinese because the simulated customers are a Chinese-speaking B2B market. A Chinese version of this README is in [`README.zh-CN.md`](README.zh-CN.md).

## Run it in 60 seconds

```bash
python ar.py seed            # 30 demo invoices
python ar.py demo            # 13-act guided demo; add --no-pause to run straight through
python ar.py verify          # prove every safety layer works on this data
python ar.py dashboard       # http://127.0.0.1:8787/
```

Time travel is the best way to see the 48-hour buffer working:

```bash
python ar.py scan --date "2026-08-25 09:00"
python ar.py scan --date "2026-08-27 10:00"   # buffer expired, account manager didn't act -> agent sends a formal statement
```

| Command | What it does |
|---|---|
| `python ar.py matrix -v` | dry-run the tiering with reasons; changes nothing |
| `python ar.py scan` | one daily scan (`--date` to simulate any day) |
| `python ar.py outbox` / `log -v` | messages the agent "sent" / audit log: who did what, to whom, and why |
| `python ar.py reply INV-2005 --text "能不能分三期"` | simulate a customer reply ("can we pay in three instalments?") |
| `python ar.py takeover INV-2010 --by Wang` | a person takes over; the agent stops immediately |
| `python ar.py report --send` | CFO weekly report (Markdown + HTML) |
| `python ar.py ask "Which high-risk invoices are over 30 days overdue?"` | read-only assistant: an LLM answers by calling read-only tools (needs `AR_LLM_PROVIDER`) |
| `python -m unittest discover -s tests` | 74 unit tests |

## How a scan works

```
read invoices ─► [breaker: batch cap] ─► tiering engine ─► classify customer replies
             ─► policy chain (L4) ─► choose action ─► [breaker: send failures] ─► channel
             ─► write back agent-owned fields ─► audit log
```

| Tier | Typical case | What the agent does |
|---|---|---|
| Low | 1–14 days overdue, small amount, clean history | WhatsApp reminder, a second one after 7 days, max 2 |
| Mid | 15–30 days, or a repeat late payer | Slack the account manager with **48 hours' priority**; only if they don't act, send a formal (not threatening) statement |
| High | > 30 days or large amount | Internal alert only. **No outbound message.** A person decides |

## Design decisions

**1. Rules decide, the model only explains.** Tiering uses thresholds, escalation uses keyword matching,
actions come from a state machine. None of it calls an LLM, so it is deterministic, auditable and immune to
prompt injection.

**2. The policy layer runs outside the LLM and can only tighten.** A system-prompt rule like "never change
payment terms" is a soft constraint: it can be ignored, injected around or forgotten in a long context.
`policy.py` reads facts and thresholds, never a prompt. Every rule can only make an outcome stricter
(escalate, block outbound), so adding a rule can never open a door that was closed.

| Rule | Effect |
|---|---|
| P-01 | Customer asks to change terms (instalments / discount / extension) → high risk + no outbound |
| P-02 | Dispute or legal signal → high risk + no outbound |
| P-03 | Amount above the red line → never handled automatically |
| P-04 | VIP customer → always a person |
| P-05 | Daily send volume above cap → global pause + alert |
| P-06 | Customer says they already paid → pause until finance reconciles |

**3. Asymmetric risk.** High risk triggers internal notifications only, so a wrong call costs finance one extra
look, never a damaged customer relationship. `verify` asserts that after a full scan the outbox holds **zero**
messages to high-risk accounts.

**4. Capability limits live in the interface, not the prompt.** The template renderer is
`render(template_id: str, variables: dict)`: there is **no free-text parameter**, so the agent can pick a
template and fill in amounts and dates but cannot write "we agree to three instalments". Outbound text then
passes a banned-phrase scan (promises such as instalments or discounts; pressure such as lawsuits or
blacklists) as defence in depth.

**5. Customer and internal channels are different types.** `CustomerChannel` (WhatsApp, Gmail to customers)
and `InternalChannel` (Slack, Gmail to the team) are separate base classes rather than one `send()` with a flag:
a flag can be set wrong, a type cannot. Internal templates sent through a customer channel are rejected.

**6. Takeover is an explicit command.** The agent does not read Slack chatter as authorisation. Chat is
context, not permission; treating it as permission would let anyone in the channel steer the system.

**7. The 48-hour buffer is a product choice.** Mid-risk accounts go to the account manager first, with a
clear deadline. The agent steps in only if nobody acts, and "account manager did not respond" is reported
weekly as a management signal. It is designed as human-machine collaboration, not machine takeover.

**8. Data permissions.** The agent's store adapter can write only its own state columns; amounts, due dates
and payment status are read-only to it and enforced in `store/base.py` (tested).

## Where the LLM is used

Two places, and neither can send, write or decide anything.

### 1. Alert summary

[`summarizer.py`](src/ar_agent/summarizer.py) turns an invoice's recent actions and customer
replies into a short summary for the **internal** high-risk alert, so finance doesn't have to read the log.

```
facts (deterministic) ─► LLM summary ─► output checks ──pass──► AI summary + original facts
                            │                   │
                            └─ timeout/error ───┴─ fail ──► original facts only
```

- **Provider-agnostic, still zero dependencies:** local Ollama (free) or Anthropic Claude, called over HTTP
  with `urllib`. Off by default; enable with `AR_LLM_PROVIDER=ollama` (or `llm.provider` in `config.json`).
- **No invented numbers:** every number in the summary must appear in the original facts, otherwise the
  summary is discarded. The model may rephrase but cannot introduce an amount or a date.
- **Injection-aware:** customer messages are passed as data inside `<facts>`, and the prompt says they are
  never instructions.
- **Fails closed:** timeouts, errors, empty or over-long output all fall back to the plain list, and the scan's
  decisions are identical either way (unit-tested). The original facts are always shown under the AI summary.

### 2. Read-only assistant (function calling)

[`assistant.py`](src/ar_agent/assistant.py) lets finance staff ask questions in plain English
(`python ar.py ask "Why is INV-2023 high risk, and what did the customer say?"`). The model answers by calling tools,
using Ollama's `tools` or Claude's `tool_use`:

| Tool | Returns |
|---|---|
| `get_overview` | open and overdue totals, count and amount per tier, top 5 customers by overdue amount |
| `list_invoices` | invoices filtered by tier, days overdue or customer, with the agent's next action |
| `get_invoice` | one invoice: tier before and after the policy layer, reasons, rules hit, whether the agent may contact the customer |
| `get_case_history` | recent actions on an invoice and the customer's replies with their detected intent |

```
question ─► LLM ─tool call─► allow-list + schema check ─► read-only tool ─► result ─► LLM ─► answer ─► number check
```

- **Read-only by construction:** there is no tool that sends, writes or marks anything, so no prompt (and no
  instruction hidden in a customer message) can make it act. Tools read a dry plan, which has no side effects.
  `verify` and the tests hash every file under `data/` before and after the tool calls.
- **Checked before it runs:** unknown tool names and bad arguments (missing, wrong type, out of range, malformed
  invoice ID) get an error result instead of being executed.
- **Bounded:** at most `llm.max_tool_calls` (6) tool calls per question.
- **No invented numbers:** every number in the answer must appear in the question or a tool result; otherwise the
  answer is withheld and the raw tool results are shown.
- Tested with a scripted fake model (11 tests) and run against a local `qwen3.5:9b`: it picks the right tools and
  filters, and it refuses requests such as "send a reminder and mark INV-2024 as paid".

Example alert from a run with `qwen3.5:9b` (translated):

> **[AI summary, for reference]** The agent sent a WhatsApp reminder on 25 Aug. On 27 Aug the customer replied
> that cash is tight this month and asked to pay in three instalments. The agent escalated on Slack on 28 Aug.
> Finance should assess whether an instalment plan is acceptable.
> *Original records:* 25 Aug WhatsApp reminder · 27 Aug customer reply (quoted) · 28 Aug Slack escalation

## Going live

Every channel has a double gate: `"live": true` in `config.json` **and** the environment variable `AR_LIVE=1`.
Either one missing means dry-run. Credentials go in `.env` (see `.env.example`); recommended order is Slack
(internal) first, then Gmail, then WhatsApp. A Google Sheets store adapter is included
(`store/gsheets_store.py`, needs `google-api-python-client`).

## Project layout

```
ar.py                     entry point: python ar.py <command>
config.json               every business threshold; finance can edit without touching code
src/ar_agent/
  tiering.py              tiering engine (pure function: facts -> tier)
  policy.py               L4 runtime policy layer, the core of the design
  inbound.py              reply classification and forced escalation (rules, no LLM)
  templates.py            L2 preset templates + outbound content gate
  store/                  L3 field-level write whitelist; CSV and Google Sheets adapters
  channels/               customer vs internal channel types
  breaker.py              L6 circuit breaker
  audit.py                L5 audit log
  orchestrator.py         daily scan + 48-hour buffer state machine
  summarizer.py           LLM use 1: alert history summary, fails closed
  assistant.py            LLM use 2: read-only assistant (function calling over read-only tools)
  report.py               weekly report and success metrics
  verify.py               live safety self-check
  demo.py                 guided demo
dashboard/index.html      local dashboard
docs/PRD.md               product spec (PRD.zh-CN.md: Chinese original)
tests/                    74 unit tests
```

## Known limits

- DSO is a proxy (amount-weighted days outstanding); true DSO needs period credit sales from the ERP. The
  weekly report labels this.
- The CSV store rewrites the whole file on each write: fine for 30–200 invoices, not for production volume.
- Reply keywords are Chinese plus a little English; multi-language is an explicit non-goal for phase one.
- No built-in scheduler; run `python ar.py scan` daily from cron or Windows Task Scheduler.

## License

MIT
