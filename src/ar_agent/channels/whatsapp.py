"""WhatsApp Business (Meta Cloud API): light-tier reminders to customers.

Live sending needs these in .env:
    WHATSAPP_PHONE_NUMBER_ID=...
    WHATSAPP_TOKEN=...

Platform rule: Meta only allows free text inside the customer's 24-hour conversation window; outside it, only pre-approved
message templates can be sent. Collection reminders are business-initiated and almost always outside the window, so every
live send uses type=template: the wording is fixed by the template approved on Meta's side, and only variables are sent.
This matches the L2 rule "pre-approved templates only": if a local template has no mapped Meta template, sending is refused (fail closed)
rather than falling back to free text.
"""
from __future__ import annotations

import json
import os
import urllib.request

from ..models import Message, SendResult
from ..templates import WHATSAPP_META_TEMPLATES
from .base import CustomerChannel

GRAPH_API = "https://graph.facebook.com/v21.0"


class UnmappedTemplate(Exception):
    """The local template has no approved Meta template mapped; refuse to send."""


def build_payload(msg: Message) -> dict:
    """Convert a local template message into a Meta template-message request body. Pure function, easy to test."""
    mapping = WHATSAPP_META_TEMPLATES.get(msg.template_id)
    if mapping is None:
        raise UnmappedTemplate(f"template {msg.template_id} has no approved WhatsApp template mapped")
    name, lang, param_names = mapping
    missing = [p for p in param_names if p not in msg.variables]
    if missing:
        raise UnmappedTemplate(f"template {msg.template_id} is missing variables: {', '.join(missing)}")
    return {
        "messaging_product": "whatsapp",
        "to": msg.to,
        "type": "template",
        "template": {
            "name": name,
            "language": {"code": lang},
            "components": [{
                "type": "body",
                "parameters": [{"type": "text", "text": str(msg.variables[p])} for p in param_names],
            }],
        },
    }


class WhatsAppChannel(CustomerChannel):
    name = "whatsapp"

    def _send_live(self, msg: Message) -> SendResult:
        payload = build_payload(msg)          # raises if unmapped; Channel.send records it as a failed send
        phone_id = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
        token = os.environ["WHATSAPP_TOKEN"]
        req = urllib.request.Request(
            f"{GRAPH_API}/{phone_id}/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        ref = (body.get("messages") or [{}])[0].get("id", "")
        return SendResult(True, self.name, msg.to, "live", "sent", ref)
