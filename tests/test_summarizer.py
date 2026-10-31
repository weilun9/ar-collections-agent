"""Unit tests for the LLM summary. Uses a fake transport: no network, no model needed."""
from __future__ import annotations

import copy
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

from ar_agent.config import Config                              # noqa: E402
from ar_agent.orchestrator import Orchestrator                  # noqa: E402
from ar_agent.seed import seed as seed_data                     # noqa: E402
from ar_agent.summarizer import Summarizer, check_summary       # noqa: E402

FACTS = [
    "2026-08-25 09:00 agent via slack: notify_am",
    "2026-08-27 10:00 agent via gmail: send_dunning",
    "2026-08-27 15:30 customer replied: \"能不能分三期付款？\"",
]


def cfg_with(provider: str, **llm) -> Config:
    raw = copy.deepcopy(Config.load(ROOT / "config.json").raw)
    raw["llm"] = {**raw.get("llm", {}), "provider": provider, **llm}
    return Config(raw)


def ollama_reply(text: str):
    def transport(url, headers, payload, timeout):
        transport.calls.append((url, payload))
        return {"message": {"content": text}}
    transport.calls = []
    return transport


@mock.patch.dict(os.environ, {"AR_LLM_PROVIDER": "", "AR_LLM_MODEL": ""})  # empty environment variables must not override the config
class TestSummarizer(unittest.TestCase):
    def test_disabled_by_default_returns_rule_list(self):
        r = Summarizer(cfg_with("none")).summarize(FACTS)
        self.assertEqual(r.source, "rule")
        self.assertIn("能不能分三期付款", r.text)

    def test_valid_summary_is_used_and_raw_facts_are_kept(self):
        t = ollama_reply("The agent sent a reconciliation email on 08-27; the customer then asked to pay in instalments, which finance needs to assess.")
        r = Summarizer(cfg_with("ollama"), transport=t).summarize(FACTS)
        self.assertEqual(r.source, "llm")
        self.assertIn("[AI summary, for reference only]", r.text)
        self.assertIn("Raw records", r.text)                      # the raw list is always kept for checking
        self.assertTrue(t.calls[0][0].endswith("/api/chat"))

    def test_customer_text_is_sent_as_data_inside_facts(self):
        t = ollama_reply("The customer asked for instalments.")
        Summarizer(cfg_with("ollama"), transport=t).summarize(FACTS)
        user_msg = t.calls[0][1]["messages"][1]["content"]
        self.assertTrue(user_msg.startswith("<facts>") and user_msg.endswith("</facts>"))
        self.assertIn("do not follow", t.calls[0][1]["messages"][0]["content"])

    def test_invented_number_is_rejected(self):
        t = ollama_reply("The customer agreed to pay 120,000 by 30 September.")
        r = Summarizer(cfg_with("ollama"), transport=t).summarize(FACTS)
        self.assertEqual(r.source, "rule")
        self.assertIn("not in the records", r.note)
        self.assertIn("120000", r.note)

    def test_overlong_or_empty_summary_is_rejected(self):
        self.assertIsNotNone(check_summary("", FACTS, 300))
        self.assertIsNotNone(check_summary("x" * 301, FACTS, 300))
        self.assertIsNone(check_summary("The customer asked for instalments; a person needs to handle it.", FACTS, 300))

    def test_transport_failure_falls_back(self):
        def boom(*_):
            raise TimeoutError("ollama not running")
        r = Summarizer(cfg_with("ollama"), transport=boom).summarize(FACTS)
        self.assertEqual(r.source, "rule")
        self.assertIn("TimeoutError", r.note)

    def test_anthropic_without_key_falls_back(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            r = Summarizer(cfg_with("anthropic"), transport=ollama_reply("x")).summarize(FACTS)
        self.assertEqual(r.source, "rule")

    def test_think_block_is_stripped(self):
        t = ollama_reply("<think>let me think first</think>The customer asked for instalments.")
        r = Summarizer(cfg_with("ollama"), transport=t).summarize(FACTS)
        self.assertNotIn("let me think first", r.text)


class TestScanWithBrokenLLM(unittest.TestCase):
    """When the model is unavailable the scan must still finish, with a fallback note in the alert."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        shutil.copy(ROOT / "config.json", self.tmp / "config.json")
        seed_data(self.tmp / "data", date(2026, 8, 25))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scan_completes_and_decisions_are_unchanged(self):
        baseline = Orchestrator(self.tmp, cfg_with("none")).daily_scan(now=datetime(2026, 8, 25, 9))

        shutil.rmtree(self.tmp / "data")
        seed_data(self.tmp / "data", date(2026, 8, 25))
        orc = Orchestrator(self.tmp, cfg_with("ollama"))

        def boom(*_):
            raise ConnectionError("down")
        orc.summarizer.transport = boom
        report = orc.daily_scan(now=datetime(2026, 8, 25, 9))

        self.assertEqual(report.by_tier, baseline.by_tier)
        self.assertEqual(len(report.sends), len(baseline.sends))


if __name__ == "__main__":
    unittest.main(verbosity=2)
