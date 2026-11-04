"""pytest configuration: makes the suite hermetic, so `pytest` passes offline with no API key.

1. `src/` goes on sys.path (the project is not pip-installed; `python ar.py` does the same).
2. Environment variables that would switch on an LLM provider or live sending are cleared for every test, so a
   developer's shell (e.g. `AR_LLM_PROVIDER=anthropic`, `AR_LIVE=1`) can never make a test call a real service.
3. Real network and SMTP are blocked. Every LLM call in the code goes through an injectable `transport`, and
   tests pass a fake one; if anything reaches the real `urllib.request.urlopen` or `smtplib.SMTP`, the test fails
   loudly instead of quietly depending on the internet.

The test files also run under the standard library (`python -m unittest discover -s tests`); they insert `src/`
on sys.path themselves and patch the environment where it matters.
"""
from __future__ import annotations

import smtplib
import sys
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

_ENV_TO_CLEAR = (
    "AR_LIVE", "AR_LLM_PROVIDER", "AR_LLM_MODEL", "OLLAMA_BASE_URL", "ANTHROPIC_API_KEY",
    "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_TOKEN", "GMAIL_USER", "GMAIL_APP_PASSWORD",
    "SLACK_WEBHOOK_URL", "SLACK_BOT_TOKEN",
)


class NetworkBlocked(RuntimeError):
    """A test tried to reach the real network. Pass a fake transport instead."""


@pytest.fixture(autouse=True)
def hermetic(monkeypatch):
    for name in _ENV_TO_CLEAR:
        monkeypatch.delenv(name, raising=False)

    def no_network(*args, **kwargs):
        raise NetworkBlocked("tests must not use the network; inject a fake transport")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    monkeypatch.setattr(smtplib, "SMTP", no_network)
    yield
