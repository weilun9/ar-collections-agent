# AR Collections Agent
### Product specification

> English version of [`PRD.zh-CN.md`](PRD.zh-CN.md).

---

## 1. Problem statement

### 1.1 Current pain points

A B2B trading company (simulated scenario: about ¥80M annual revenue, 300+ business customers, 30-60 day payment terms) has a
two-person finance team tracking receivables, and 20 sales staff each chasing payments from their own customers.

| Pain point | What happens | Impact |
|---|---|---|
| Slow follow-up | Finance exports Excel by hand every week and decides which invoices to chase | First reminder goes out **5-7 days** after an invoice is overdue, on average |
| Missed invoices | 300+ customers watched by hand; small and mid-sized invoices are often forgotten | About **8-12%** of overdue invoices get no follow-up at all each month |
| One-size-fits-all chasing | Same wording and channel regardless of relationship or amount | Some VIP customers get blunt reminders, which hurts the relationship |
| No overall view for management | No single view of overdue health; the CFO relies on monthly verbal updates from finance | Cash-flow risks surface late, with little to base decisions on |

### 1.2 Why now

- Most companies already use WhatsApp Business for day-to-day customer contact; collections can reuse that channel instead of asking customers to learn another system
- An LLM can take in how long an invoice has been overdue and how this customer usually pays, and support finer tiering than "N days overdue, send fixed template"
- Finance and sales time should go to large or high-risk invoices that need negotiation, not to sending reminder emails

### 1.3 Non-goals

Clear boundaries:
- ❌ No automatic debiting, discounts or instalment promises: any action that **changes payment terms** needs a human decision
- ❌ Does not replace legal or collection-agency enforcement (lawsuits, third-party collection)
- ❌ No multi-language or multi-currency support in phase 1 (validate one market first)

---

## 2. Users and user stories

### 2.1 Personas

| Role | Core need |
|---|---|
| Finance officer (main operator) | Move from "watching Excel every day" to "only handling the cases the system flags" |
| Account manager / sales | Be told before a customer is chased automatically, and contact the customer first when needed to protect the relationship |
| CFO / finance lead | An overdue health report available at any time, without chasing finance for it |
| Business customer (being chased) | Clear, professional reminders that do not feel like harassment |

### 2.2 User stories

```
As a finance officer,
I want the system to scan receivables every day and tier them by risk,
so that I no longer export Excel by hand and judge each line myself.

As a finance officer,
I want light-tier overdue invoices followed up automatically by the agent over WhatsApp / email,
so that I can spend my time on large or high-risk cases that need human judgement.

As an account manager,
I want a Slack notice before the agent contacts any of my customers who reach the medium-risk tier,
so that I can choose to contact them myself first instead of finding out after the system has acted.

As the CFO,
I want an accounts-receivable health report every week, automatically,
so that I can spot cash-flow risks early without asking finance for numbers each time.

As a business customer,
I want reminders that are professional and clear (amount, due date, payment link),
so that a single automated reminder does not sour the relationship.
```

---

## 3. Architecture and integrations

### 3.1 Overall flow

```
[Data source] Google Sheets (receivables table standing in for the finance system)
        ↓ scheduled daily scan
[Tiering engine] days overdue + amount + customer history → risk tier (light / medium / high)
        ↓
   ┌────┴─────────────┬──────────────────────────┐
Light: auto follow-up   Medium: sales steps in first   High: straight to people + Slack alert
   ↓                      ↓                              ↓
WhatsApp/Gmail          Slack notice to account manager  Slack + email to the finance lead
automatic reminder      + 48-hour buffer                 (nothing sent to the customer automatically)
        ↓
[Record layer] every action written back to Sheets (state, channel, time, content)
        ↓
[Report layer] weekly report generated every Monday and emailed to the CFO
```

### 3.2 Integrations

| System | Purpose | Data flow |
|---|---|---|
| **Google Sheets** (instead of a CRM / finance system) | Receivables master data, customer payment history, agent action log | read + write |
| **WhatsApp Business API** | Reminders to customers (light tier) | write only (send); replies handled by people |
| **Gmail API** | Formal dunning letters to customers (written record for medium / high risk); weekly report to the CFO | write only |
| **Slack** | Internal alerts: "your customer is now medium risk" to the account manager, "a high-risk case needs you" to the finance lead | write only (notifications), no automated decisions |

**Design principle**: customer-facing channels (WhatsApp / Gmail) and internal alert channels (Slack) are strictly separate, so even
if the agent misjudges, the worst case is an extra internal notification, not a wrong message to a customer.

---

## 4. Tiering decision matrix (core design)

This is the heart of the design: risk decides what the agent may do on its own and what must go to a person.

| Tier | Trigger | What the agent does on its own | How people step in |
|---|---|---|---|
| **🟢 Light** (Auto) | 1-14 days overdue, amount < ¥50,000, and no late-payment history | Sends a first friendly WhatsApp reminder automatically; a second one after 7 days without a response | No action needed; finance can spot-check in the weekly report |
| **🟡 Medium** (Notify-then-Act) | 15-30 days overdue, or amount ¥50,000-¥200,000, or one late payment in the customer's history | Notifies the account manager on Slack first and **waits 48 hours**; only if there is no response does the agent send a formal dunning letter by Gmail | Within 48 hours the account manager can take over (mark it as handled by a person), and the agent stops |
| **🔴 High risk** (Human-Only) | More than 30 days overdue, or amount > ¥200,000, or the customer asks to change payment terms (extension, discount, etc.) | **Sends nothing to the customer**; only notifies the finance lead and CFO on Slack and by email, with invoice details and a summary of the contact history | A person must decide the next step (keep collecting / start legal action / negotiate a payment plan) |

### 4.1 Key design: why does "medium" have a 48-hour buffer?

This is not a technical limit but a **product decision**: the agent is not meant to act before people, but to give people priority
and step in only when nobody responds. It is positioned as human-AI collaboration, not machine takeover.

### 4.2 Key design: any "change of payment terms" keyword escalates automatically

When the agent parses a customer reply (WhatsApp / email) and detects a request such as "can we pay in instalments", "can you give
a discount" or "can we pay later", **whatever the current tier, the invoice is forced to high risk, automation stops and it goes to a
person**. The rule does not rely on the agent judging whether the request is reasonable: a matching keyword category escalates
unconditionally. This is a hard rule in the runtime policy layer, not a soft constraint in a system prompt, so this complex logic
cannot be bypassed.

---

## 5. Risk boundaries and governance (seven-layer framework)

| Layer | Design in this project |
|---|---|
| L1 System prompt | States the agent's role: "you only send standard reminders; any change to payment terms must go to a person", but **this prompt is not relied on as the only defence** |
| L2 Tool design | `send_reminder(customer_id, tier)` can only send pre-approved templates and cannot generate free text promising payment terms; `escalate_to_human(reason)` is a separate tool with a single permission |
| L3 Agent IAM | The agent's service account can read everything in Sheets but only write status fields; it has no write permission on amount or payment-term fields |
| L4 Runtime policy (core) | Keywords about instalments / discounts / extensions force escalation; more than 50 sends in a day pauses sending and notifies finance; any invoice above ¥200,000 can never be handled automatically by the agent, whatever its tier |
| L5 Observability | Every agent action (send time, channel, content, trigger) goes into a separate log table, so finance and the CFO can always check whether a reminder was sent by the agent or by a person |
| L6 Circuit breakers | A single scan handles at most 200 invoices and defers the rest; 5 consecutive WhatsApp send failures pause sending and raise an alert |
| L7 Compliance | Access to customer financial data (amounts, contact details) is limited to finance roles; collection messages must follow local debt-collection rules (for example, no pressure wording) |

---

## 6. Success metrics

### 6.1 Core metrics (North Star + supporting metrics)

- **North Star**: reduction in days sales outstanding (DSO)
- Supporting metrics:
  - Average time from going overdue to the first reminder (target: from 5-7 days down to < 24 hours)
  - Coverage of overdue invoices (target: from 88-92% up to 100%, i.e. none missed)
  - Share of actions handled by the agent vs by people (target: 80% of the light tier handled by the agent)
  - Customer complaint rate caused by collection (target: no higher than with manual collection)

### 6.2 Guardrail metrics (so efficiency does not cost relationships)

- Wrong-escalation rate for high / medium risk cases (share that should have gone to a person but was handled automatically; target: 0)
- Number of times a VIP customer (custom tag) was chased automatically by the agent (target: 0; VIPs always go to a person by default)

---

## 7. MVP scope and roadmap

| Phase | Scope | Timing |
|---|---|---|
| **MVP** | One simulated customer book (20-30 simulated invoices) + light / medium / high tiers + WhatsApp / Gmail / Slack connected | — |
| **Phase 2** | Learn from customer payment history (tier thresholds adjusted from historical data instead of hard-coded rules) | later |
| **Phase 3** | Multi-currency / multi-language support, connection to a real ERP / finance system | later |

---

## 8. Design trade-offs FAQ

**Q: What if the agent misjudges and marks a perfectly fine customer as high risk?**
A: The high-risk tier only notifies people and triggers no automatic customer-facing action, so a misjudgement costs finance a second
look rather than harming a customer. This asymmetric risk control is deliberate: a false alarm adds some human review; a miss could
let automation damage a customer relationship.

**Q: What if the account manager does not respond to a medium case within 48 hours?**
A: The agent sends a **formal but restrained** dunning letter from a pre-approved template (not pressure wording), and records "account
manager did not respond" in the weekly report. That is a management signal in itself, showing how promptly that account manager responds.

**Q: How is this different from plain RPA (rule automation)?**
A: RPA means "N days overdue, send a fixed template". This agent adds **dynamic tiering** (amount, history and the meaning of customer
replies) plus **keyword-driven forced escalation to people**, on top of a runtime policy layer that is independent of the LLM. RPA cannot
do the first two, and a purely prompt-driven agent cannot keep the risk under control; this design sits between the two.

---

*(This project uses simulated data and is not connected to any real customers.)*
