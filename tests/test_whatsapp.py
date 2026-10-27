"""WhatsApp 渠道：live 发送必须走 Meta 预审批模板，且无映射时拒绝发送。"""
from __future__ import annotations

import string
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ar_agent.channels.whatsapp import UnmappedTemplate, build_payload  # noqa: E402
from ar_agent.models import Message                                     # noqa: E402
from ar_agent.templates import TEMPLATES, WHATSAPP_META_TEMPLATES       # noqa: E402

VARS = {
    "contact_name": "王经理", "company": "示例公司", "invoice_id": "INV-001",
    "amount": "12,000.00", "due_date": "2026-10-01", "payment_url": "https://pay.example/INV-001",
}


def _msg(template_id: str, variables: dict | None = None) -> Message:
    return Message("whatsapp", "+6591234567", "", "rendered body", template_id,
                   "INV-001", "C001", "customer", variables=dict(VARS if variables is None else variables))


class WhatsAppPayloadTests(unittest.TestCase):
    def test_every_customer_whatsapp_template_is_mapped(self):
        for tid, tpl in TEMPLATES.items():
            if tpl.channel == "whatsapp" and tpl.audience == "customer":
                self.assertIn(tid, WHATSAPP_META_TEMPLATES, tid)

    def test_mapped_params_match_local_template_placeholders(self):
        for tid, (_, _, params) in WHATSAPP_META_TEMPLATES.items():
            fields = {f for _, f, _, _ in string.Formatter().parse(TEMPLATES[tid].body) if f}
            self.assertEqual(set(params), fields, tid)

    def test_payload_is_template_message_never_free_text(self):
        p = build_payload(_msg("WA_LOW_1"))
        self.assertEqual(p["type"], "template")
        self.assertNotIn("text", p)
        params = [x["text"] for x in p["template"]["components"][0]["parameters"]]
        self.assertEqual(params[0], "王经理")
        self.assertIn("INV-001", params)

    def test_unmapped_template_fails_closed(self):
        with self.assertRaises(UnmappedTemplate):
            build_payload(_msg("MAIL_MID_DUNNING"))

    def test_missing_variable_fails_closed(self):
        with self.assertRaises(UnmappedTemplate):
            build_payload(_msg("WA_LOW_1", {"contact_name": "王经理"}))


if __name__ == "__main__":
    unittest.main()
