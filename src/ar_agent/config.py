"""配置加载。所有阈值来自 config.json，代码里不硬编码业务数字。"""
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
        """按 'policy.daily_send_cap' 这种路径取值。"""
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    # ---- 便捷访问 ----
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
        """双重闸门：config 里 live=true **且** 环境变量 AR_LIVE=1，才会真的对外发送。

        任何一个没开，一律走 dry-run 写 outbox。这样即使误改了配置文件，
        没有显式设置 AR_LIVE 也不会对客户发出真实消息。
        """
        configured = bool(self.get(f"channels.{name}.live", False))
        env_gate = os.environ.get("AR_LIVE", "0") == "1"
        return configured and env_gate
