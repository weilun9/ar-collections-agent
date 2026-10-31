"""Configuration loading. Every threshold comes from config.json; no business numbers are hard-coded."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
OUTBOX_DIR = DATA_DIR / "outbox"
REPORTS_DIR = ROOT / "reports"


@dataclass
class Config:
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or ROOT / "config.json"
        with open(path, encoding="utf-8") as fh:
            return cls(json.load(fh))

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    def get(self, dotted: str, default: Any = None) -> Any:
        """Look up a dotted path such as 'policy.daily_send_cap'."""
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    # ---- Shortcuts ----
    @property
    def company(self) -> dict[str, Any]:
        return self.raw["company"]

    @property
    def tiering(self) -> dict[str, Any]:
        return self.raw["tiering"]

    @property
    def cadence(self) -> dict[str, Any]:
        return self.raw["cadence"]

    @property
    def policy(self) -> dict[str, Any]:
        return self.raw["policy"]

    @property
    def breaker(self) -> dict[str, Any]:
        return self.raw["breaker"]

    def channel_is_live(self, name: str) -> bool:
        """Two gates: live=true in config **and** AR_LIVE=1 in the environment are both required to send for real.

        If either is missing, everything goes to the dry-run outbox. So even a mistaken config edit
        cannot send a real message to a customer unless AR_LIVE is set explicitly.
        """
        configured = bool(self.get(f"channels.{name}.live", False))
        env_gate = os.environ.get("AR_LIVE", "0") == "1"
        return configured and env_gate
