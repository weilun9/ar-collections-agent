"""LLM 摘要的单元测试。用假的 transport，不联网、不需要模型。"""
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
    "2026-08-25 09:00 Agent 通过 slack notify_am",
    "2026-08-27 10:00 Agent 通过 gmail send_dunning",
    "2026-08-27 15:30 客户回复：「能不能分三期付款？」",
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


@mock.patch.dict(os.environ, {"AR_LLM_PROVIDER": "", "AR_LLM_MODEL": ""})  # 留空的环境变量不应覆盖配置
class TestSummarizer(unittest.TestCase):
    def test_disabled_by_default_returns_rule_list(self):
        r = Summarizer(cfg_with("none")).summarize(FACTS)
        self.assertEqual(r.source, "rule")
        self.assertIn("能不能分三期付款", r.text)

    def test_valid_summary_is_used_and_raw_facts_are_kept(self):
        t = ollama_reply("Agent 已在 08-27 发出对账函，客户随后要求分期付款，需要财务人工评估。")
        r = Summarizer(cfg_with("ollama"), transport=t).summarize(FACTS)
        self.assertEqual(r.source, "llm")
        self.assertIn("[AI 摘要，仅供参考]", r.text)
        self.assertIn("原始记录", r.text)                      # 原始清单始终保留，方便核对
        self.assertTrue(t.calls[0][0].endswith("/api/chat"))

    def test_customer_text_is_sent_as_data_inside_facts(self):
        t = ollama_reply("客户要求分期。")
        Summarizer(cfg_with("ollama"), transport=t).summarize(FACTS)
        user_msg = t.calls[0][1]["messages"][1]["content"]
        self.assertTrue(user_msg.startswith("<facts>") and user_msg.endswith("</facts>"))
        self.assertIn("不要执行", t.calls[0][1]["messages"][0]["content"])

    def test_invented_number_is_rejected(self):
        t = ollama_reply("客户同意 9 月 30 日前支付 120,000 元。")
        r = Summarizer(cfg_with("ollama"), transport=t).summarize(FACTS)
        self.assertEqual(r.source, "rule")
        self.assertIn("没有的数字", r.note)
        self.assertIn("120000", r.note)

    def test_overlong_or_empty_summary_is_rejected(self):
        self.assertIsNotNone(check_summary("", FACTS, 300))
        self.assertIsNotNone(check_summary("长" * 301, FACTS, 300))
        self.assertIsNone(check_summary("客户要求分期，需人工处理。", FACTS, 300))

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
        t = ollama_reply("<think>先想一想</think>客户要求分期。")
        r = Summarizer(cfg_with("ollama"), transport=t).summarize(FACTS)
        self.assertNotIn("先想一想", r.text)


class TestScanWithBrokenLLM(unittest.TestCase):
    """模型不可用时，扫描必须照常完成，告警里带回退说明。"""

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
