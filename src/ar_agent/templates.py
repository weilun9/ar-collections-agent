"""L2 tool design (product spec §5): "only pre-approved templates can be sent; no freely generated text promising payment terms".

Two layers, both required:

  **Layer 1: the interface offers no way in for free text.**
  render() takes (template_id, dict of variables) and has no body/text/content parameter.
  The agent can choose a template and fill variables (amount, date, invoice number), but cannot produce a sentence such as
  "we agree you may pay in three instalments". It is not that we ask it not to; **it cannot**.

  **Layer 2: scan the rendered text for forbidden wording.**
  In case a template is edited by mistake or a variable injects risky wording, there is one more guard before sending.
  Defence in depth: assume layer 1 will be bypassed and ask again what the worst possible message would be.

Wording principles: professional, restrained, complete (amount / due date / payment link), and no pressure wording
(spec §1 user story "a single automated reminder should not sour the relationship", and §5 L7 compliance).
"""
from __future__ import annotations

import re
from dataclasses import dataclass


class TemplateViolation(Exception):
    """Raised when rendered text contains forbidden wording; sending stops."""


@dataclass(frozen=True)
class Template:
    id: str
    channel: str
    audience: str          # customer / internal
    subject: str
    body: str
    note: str = ""


#: Forbidden wording: anything suggesting a change to payment terms, or pressure.
#: The first kind maps to the spec's non-goals (changing payment terms needs a human decision);
#: the second maps to §5 L7 (collection messages must follow local debt-collection rules; no pressure wording).
FORBIDDEN_PATTERNS: list[tuple[str, str]] = [
    (r"分\s*期|installment", "must not promise instalments"),
    (r"打\s*折|折扣|减免|优惠|discount|waive", "must not promise a discount or waiver"),
    (r"延\s*期|延\s*后|宽\s*限|展\s*期", "must not promise an extension"),
    (r"免除|豁免|一笔勾销", "must not promise to write off the debt"),
    (r"起诉|上门|曝光|黑名单|失信|后果自负|严重后果", "must not use pressure or threats"),
    (r"立即|马上|务必|限你", "must not use coercive deadlines"),
]

_CODE_FENCE = "```"

TEMPLATES: dict[str, Template] = {
    # ---------------- To customers (light tier: friendly WhatsApp reminder; customer-facing text stays in Chinese) ----------------
    "WA_LOW_1": Template(
        id="WA_LOW_1",
        channel="whatsapp",
        audience="customer",
        subject="",
        body=(
            "{contact_name} 您好，我是{company}的财务助手。\n\n"
            "温馨提醒：发票 {invoice_id}（金额 ¥{amount}）的付款日为 {due_date}，"
            "目前显示尚未收到款项。\n\n"
            "如已完成付款，请忽略本条消息，或把付款凭证发给我们核对即可。\n"
            "付款链接：{payment_url}\n\n"
            "如对账目有任何疑问，回复本条消息即可，会有同事跟进。感谢您的配合。"
        ),
        note="First friendly reminder: light tone, with a clear 'ignore this if already paid' way out",
    ),
    "WA_LOW_2": Template(
        id="WA_LOW_2",
        channel="whatsapp",
        audience="customer",
        subject="",
        body=(
            "{contact_name} 您好，关于发票 {invoice_id}（金额 ¥{amount}，付款日 {due_date}），"
            "系统显示仍未收到款项，再次与您确认一下。\n\n"
            "付款链接：{payment_url}\n\n"
            "如付款安排上需要沟通，或账目需要核对，回复本条消息，"
            "我们会安排对接同事直接与您联系。"
        ),
        note="Second reminder, still no pressure. The 'if you need to discuss' wording is deliberate: it invites the customer to state a request, which triggers keyword escalation to a person",
    ),

    # ---------------- To customers (medium tier: formal Gmail dunning letter) ----------------
    "MAIL_MID_DUNNING": Template(
        id="MAIL_MID_DUNNING",
        channel="gmail",
        audience="customer",
        subject="付款提醒 · 发票 {invoice_id} · {company}",
        body=(
            "{contact_name} 您好，\n\n"
            "现就以下应收款项与贵司确认：\n\n"
            "    发票编号：{invoice_id}\n"
            "    金额：¥{amount}\n"
            "    开票日期：{issue_date}\n"
            "    约定付款日：{due_date}\n"
            "    逾期天数：{days_overdue} 天\n\n"
            "截至本函发出时，我方尚未收到上述款项。烦请贵司财务同事协助核实付款状态。\n\n"
            "付款链接：{payment_url}\n\n"
            "若款项已在处理中、或贵司对账目存在疑问，请直接回复本邮件说明情况，"
            "我方会安排对应同事跟进核对。\n\n"
            "感谢贵司一直以来的合作。\n\n"
            "{company} 财务部\n"
            "（本邮件由应收账款系统按既定流程发出，仅作对账提醒之用）"
        ),
        note="Formal but restrained. Spec Q2: sent when the account manager has not responded; not a pressure letter",
    ),

    # ---------------- Internal (Slack: account-manager buffer notice) ----------------
    "SLACK_AM_BUFFER": Template(
        id="SLACK_AM_BUFFER",
        channel="slack",
        audience="internal",
        subject="",
        body=(
            ":large_yellow_circle: *Medium-risk invoice · decide whether to step in first*\n\n"
            "• Customer: *{customer_name}* (account manager: {account_manager})\n"
            "• Invoice: {invoice_id}   Amount: ¥{amount}\n"
            "• Due date: {due_date}   Overdue: *{days_overdue} days*\n"
            "• Tier reasons: {reasons}\n\n"
            "*You have {buffer_hours} hours of priority* (until {buffer_until}).\n"
            "Until then you can contact the customer yourself; to take over, run:\n"
            + _CODE_FENCE + "\nar takeover {invoice_id} --by {account_manager}\n" + _CODE_FENCE + "\n"
            "If there is still no response by then, the system will send the standard formal reconciliation email "
            "and record this invoice in the weekly report as 'account manager did not respond'."
        ),
        note="Buffer notice. It states what happens when the time runs out, so the account manager knows the consequence instead of being silently overridden",
    ),

    # ---------------- Internal (Slack: high-risk alert) ----------------
    "SLACK_HIGH_ALERT": Template(
        id="SLACK_HIGH_ALERT",
        channel="slack",
        audience="internal",
        subject="",
        body=(
            ":red_circle: *High-risk invoice · human decision needed*\n\n"
            "• Customer: *{customer_name}* (account manager: {account_manager})\n"
            "• Invoice: {invoice_id}   Amount: *¥{amount}*\n"
            "• Due date: {due_date}   Overdue: *{days_overdue} days*\n"
            "• Escalation reasons: {reasons}\n"
            "• Policy rules hit: {policy_hits}\n\n"
            "*The system has sent nothing to this customer and will not send anything automatically.*\n"
            "Contact history summary: {history}\n\n"
            "Please decide the next step: keep collecting / start legal action / negotiate a payment plan."
        ),
        note="High risk is notify-only. 'The system has sent nothing' is bold on purpose so finance can confirm at a glance that the customer was not contacted",
    ),

    # ---------------- Internal (Gmail: written high-risk notice to the finance lead) ----------------
    "MAIL_FINANCE_ALERT": Template(
        id="MAIL_FINANCE_ALERT",
        channel="gmail",
        audience="internal",
        subject="[High-risk receivable] {customer_name} · {invoice_id} · ¥{amount}",
        body=(
            "The following invoice has been marked high risk and needs a human decision:\n\n"
            "    Customer: {customer_name}\n"
            "    Account manager: {account_manager}\n"
            "    Invoice number: {invoice_id}\n"
            "    Amount: ¥{amount}\n"
            "    Due date: {due_date}\n"
            "    Days overdue: {days_overdue}\n\n"
            "Escalation reasons:\n{reasons}\n\n"
            "Runtime policy rules hit:\n{policy_hits}\n\n"
            "Contact history summary:\n{history}\n\n"
            "The system has sent no automated message to this customer; any next step must be taken by a person.\n"
        ),
        note="A written record for the finance lead, alongside the Slack alert: Slack scrolls away, email does not",
    ),

    # ---------------- Internal (Slack: circuit-breaker / policy pause alert) ----------------
    "SLACK_BREAKER": Template(
        id="SLACK_BREAKER",
        channel="slack",
        audience="internal",
        subject="",
        body=(
            ":rotating_light: *AR collections agent paused*\n\n"
            "• Reason: {reason}\n"
            "• Time: {timestamp}\n"
            "• Channel affected: {channel}\n\n"
            "Customer sending has stopped. After investigating and confirming the channel is back, run:\n"
            + _CODE_FENCE + "\nar reset-breaker --channel {channel}\n" + _CODE_FENCE + "\n"
            "(No automatic recovery on purpose: a tripped breaker means something is wrong and someone should look.)"
        ),
        note="Circuit-breaker alert",
    ),
}


def _guard(text: str, template_id: str) -> None:
    """Content gate before sending (second layer of defence in depth)."""
    for pattern, why in FORBIDDEN_PATTERNS:
        m = re.search(pattern, text)
        if m:
            raise TemplateViolation(
                f"template {template_id} rendered forbidden wording '{m.group(0)}': {why}"
            )


#: WhatsApp template mapping: local template id -> (approved Meta template name, language, variable names in {{1}}{{2}}... order).
#: Business-initiated reminders are almost always outside the 24-hour conversation window, where Meta only accepts pre-approved templates,
#: so every WhatsApp message to a customer is sent as a template. The template text on Meta's side must match the body in TEMPLATES above.
WHATSAPP_META_TEMPLATES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "WA_LOW_1": ("ar_reminder_1", "zh_CN",
                 ("contact_name", "company", "invoice_id", "amount", "due_date", "payment_url")),
    "WA_LOW_2": ("ar_reminder_2", "zh_CN",
                 ("contact_name", "invoice_id", "amount", "due_date", "payment_url")),
}


def render(template_id: str, variables: dict) -> tuple[str, str]:
    """Render a template and return (subject, body).

    Note the signature: **there is no free-text parameter**. The agent can only choose a template_id and supply structured variables.
    That is the point of L2: the capability boundary is in the interface, not in a prompt.
    """
    tpl = TEMPLATES.get(template_id)
    if tpl is None:
        raise KeyError(
            f"unknown template: {template_id} (templates are added after finance/compliance review; the agent cannot create them)"
        )

    safe = {k: ("" if v is None else str(v)) for k, v in variables.items()}
    try:
        subject = tpl.subject.format(**safe)
        body = tpl.body.format(**safe)
    except KeyError as exc:
        raise KeyError(f"template {template_id} is missing a variable: {exc}") from exc

    if tpl.audience == "customer":
        # Only customer-facing text is checked for forbidden wording: words like 'instalments' or 'lawsuit' are normal in internal alerts,
        # because finance needs to see them to decide. Different standards for internal and external text are deliberate.
        _guard(body, template_id)
        _guard(subject, template_id)

    return subject, body


def catalog() -> list[dict]:
    return [
        {"id": t.id, "channel": t.channel, "audience": t.audience, "note": t.note}
        for t in TEMPLATES.values()
    ]
