"""Read-only assistant (function calling). Uses a scripted fake transport: no network, no model needed."""
from __future__ import annotations

import copy
import hashlib
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ar_agent.assistant import TOOL_NAMES, TOOLS, Assistant, ToolBox, check_answer, validate_args  # noqa: E402
from ar_agent.config import Config                                                                 # noqa: E402
from ar_agent.seed import seed as seed_data                                                        # noqa: E402

NOW = datetime(2026, 8, 25, 9)


def cfg_with(provider: str, **llm) -> Config:
    raw = copy.deepcopy(Config.load(ROOT / "config.json").raw)
    raw["llm"] = {**raw.get("llm", {}), "provider": provider, **llm}
    return Config(raw)


def scripted(*responses):
    """Fake transport that returns the given responses in order and records every request."""
    def transport(url, headers, payload, timeout):
        transport.calls.append((url, copy.deepcopy(payload)))
        return responses[min(len(transport.calls), len(responses)) - 1]
    transport.calls = []
    return transport


def ollama_tool(name, **args):
    return {"message": {"role": "assistant", "content": "",
                        "tool_calls": [{"function": {"name": name, "arguments": args}}]}}


def ollama_text(text):
    return {"message": {"role": "assistant", "content": text}}


def data_digest(data_dir: Path) -> dict[str, str]:
    return {str(p.relative_to(data_dir)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(data_dir.rglob("*")) if p.is_file()}


@mock.patch.dict(os.environ, {"AR_LLM_PROVIDER": "", "AR_LLM_MODEL": ""})  # empty variables must not override the config
class TestAssistant(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        shutil.copy(ROOT / "config.json", self.tmp / "config.json")
        seed_data(self.tmp / "data", date(2026, 8, 25))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def assistant(self, provider="ollama", transport=None, **llm):
        return Assistant(self.tmp, cfg_with(provider, **llm), transport=transport, now=NOW)

    # ---- the tools themselves ----
    def test_tools_are_read_only_and_leave_data_unchanged(self):
        self.assertEqual(TOOL_NAMES, {"get_overview", "list_invoices", "get_invoice", "get_case_history"})
        for name in TOOL_NAMES:
            for verb in ("send", "write", "update", "mark", "takeover", "release", "delete", "pay"):
                self.assertNotIn(verb, name)
        box = ToolBox(self.tmp, cfg_with("none"), NOW)
        before = data_digest(self.tmp / "data")                    # after setup: only the tool calls are measured
        for t in TOOLS:
            args = {"invoice_id": "INV-2019"} if "invoice_id" in t["parameters"]["required"] else {}
            result, ok = box.call(t["name"], args)
            self.assertTrue(ok, f"{t['name']}: {result}")
        self.assertEqual(before, data_digest(self.tmp / "data"), "a read-only tool changed the data files")

    def test_tool_results_match_the_tiering(self):
        box = ToolBox(self.tmp, cfg_with("none"), NOW)
        inv, _ = box.call("get_invoice", {"invoice_id": "INV-2019"})
        self.assertEqual((inv["tier"], inv["amount"], inv["days_overdue"]), ("high", 260000.0, 40))
        self.assertFalse(inv["agent_may_contact_customer"])
        high, _ = box.call("list_invoices", {"tier": "high", "min_days_overdue": 30})
        self.assertEqual({r["invoice_id"] for r in high["invoices"]}, {"INV-2016", "INV-2017", "INV-2019", "INV-2020"})

    def test_bad_arguments_are_refused_before_running(self):
        bad = [
            ("get_invoice", {}),                                   # missing required argument
            ("get_invoice", {"invoice_id": "INV-1; DROP TABLE"}),  # fails the pattern
            ("list_invoices", {"tier": "urgent"}),                 # not in the enum
            ("list_invoices", {"limit": 999}),                     # out of range
            ("list_invoices", {"amount": 1}),                      # unknown argument
            ("send_whatsapp", {"invoice_id": "INV-2001"}),         # not on the allow-list
        ]
        for name, args in bad:
            clean, error = validate_args(name, args)
            self.assertIsNone(clean, f"{name}{args} should be refused")
            self.assertTrue(error)
        clean, error = validate_args("list_invoices", '{"min_days_overdue": "30"}')
        self.assertIsNone(error)
        self.assertEqual(clean, {"min_days_overdue": 30})          # JSON-string arguments and numeric strings are accepted

    # ---- the function-calling loop ----
    def test_ollama_tool_loop_answers_from_tool_results(self):
        t = scripted(ollama_tool("get_invoice", invoice_id="INV-2019"),
                     ollama_text("INV-2019 is high risk: ¥260,000, 40 days overdue. The agent may not contact the customer."))
        res = self.assistant(transport=t).ask("What is the status of INV-2019?")
        self.assertEqual(res.source, "llm", res.note)
        self.assertEqual([(c.name, c.ok) for c in res.calls], [("get_invoice", True)])
        first, second = t.calls[0][1], t.calls[1][1]
        self.assertEqual({tl["function"]["name"] for tl in first["tools"]}, TOOL_NAMES)
        self.assertEqual(second["messages"][-1]["role"], "tool")
        self.assertEqual(second["messages"][-1]["tool_name"], "get_invoice")
        self.assertIn("260000", second["messages"][-1]["content"])

    def test_anthropic_tool_use_round_trip(self):
        t = scripted(
            {"content": [{"type": "tool_use", "id": "tu_1", "name": "list_invoices",
                          "input": {"tier": "high", "min_days_overdue": 30}}], "stop_reason": "tool_use"},
            {"content": [{"type": "text", "text": "INV-2020, INV-2017, INV-2019 and INV-2016 are high risk and over 30 days overdue."}]},
        )
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            res = self.assistant("anthropic", transport=t).ask("Which high-risk invoices are over 30 days overdue?")
        self.assertEqual(res.source, "llm", res.note)
        payload = t.calls[1][1]
        self.assertIn("input_schema", payload["tools"][0])
        block = payload["messages"][-1]["content"][0]
        self.assertEqual((block["type"], block["tool_use_id"], block["is_error"]), ("tool_result", "tu_1", False))

    def test_unlisted_tool_is_not_executed(self):
        t = scripted(ollama_tool("send_whatsapp", invoice_id="INV-2001", text="pay now"),
                     ollama_text("I cannot send messages; a person must contact the customer."))
        assistant = self.assistant(transport=t)
        before = data_digest(self.tmp / "data")
        res = assistant.ask("Send INV-2001 a reminder now.")
        self.assertEqual(res.source, "llm")
        self.assertFalse(res.calls[0].ok)
        self.assertIn("unknown tool", t.calls[1][1]["messages"][-1]["content"])
        self.assertEqual(before, data_digest(self.tmp / "data"))
        self.assertEqual(list((self.tmp / "data" / "outbox").glob("*.json")), [], "nothing may reach the outbox")

    def test_tool_call_limit_stops_the_loop(self):
        t = scripted(ollama_tool("get_overview"))                  # the model never stops calling tools
        res = self.assistant(transport=t, max_tool_calls=3).ask("Overview?")
        self.assertEqual(res.source, "withheld")
        self.assertEqual(len(res.calls), 3)
        self.assertIn("stopped after 3", res.note)

    def test_invented_number_withholds_the_answer(self):
        t = scripted(ollama_tool("get_invoice", invoice_id="INV-2019"),
                     ollama_text("INV-2019 is ¥260,000 and the customer promised to pay 99,999 next week."))
        res = self.assistant(transport=t).ask("What is the status of INV-2019?")
        self.assertEqual(res.source, "withheld")
        self.assertIn("99999", res.note)
        self.assertIn("get_invoice", res.answer)                   # the raw tool results are shown instead

    def test_numbers_from_the_question_are_allowed(self):
        self.assertIsNone(check_answer("None are over 90 days overdue.", ["How many invoices are over 90 days overdue?"]))
        self.assertIsNotNone(check_answer("", ["anything"]))

    def test_disabled_without_a_provider(self):
        t = scripted(ollama_text("unused"))
        res = self.assistant("none", transport=t).ask("Overview?")
        self.assertEqual(res.source, "disabled")
        self.assertEqual(t.calls, [])

    def test_transport_failure_is_reported_not_raised(self):
        def boom(*_):
            raise ConnectionError("ollama not running")
        res = self.assistant(transport=boom).ask("Overview?")
        self.assertEqual(res.source, "error")
        self.assertIn("ConnectionError", res.note)


if __name__ == "__main__":
    unittest.main(verbosity=2)
