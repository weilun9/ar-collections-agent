"""L6 circuit breakers (product spec §5).

Two breakers:
  1. A single scan handles at most N invoices and defers the rest (stops one run from sweeping the whole table);
  2. N consecutive send failures pause the channel and raise an alert (stops a dead channel or revoked credentials from spinning).

Breaker state is persisted in data/breaker_state.json and must survive restarts;
otherwise the "5 failures in a row" counter resets on every scan and the breaker never trips.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

from .config import Config


@dataclass
class BreakerState:
    consecutive_failures: dict[str, int] = field(default_factory=dict)
    tripped: dict[str, str] = field(default_factory=dict)     # channel -> time it tripped
    sends_by_date: dict[str, int] = field(default_factory=dict)


class CircuitBreaker:
    def __init__(self, path: Path, cfg: Config):
        self.path = Path(path)
        self.cfg = cfg
        self.state = self._load()

    def _load(self) -> BreakerState:
        if self.path.exists():
            with open(self.path, encoding="utf-8") as fh:
                return BreakerState(**json.load(fh))
        return BreakerState()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(asdict(self.state), fh, ensure_ascii=False, indent=2)

    # ---- Breaker 1: batch size per scan ----
    def batch(self, invoices: list) -> tuple[list, list]:
        """Return (this batch, deferred to the next batch)."""
        cap = self.cfg.breaker["max_invoices_per_scan"]
        return invoices[:cap], invoices[cap:]

    # ---- Breaker 2: consecutive send failures ----
    def is_tripped(self, channel: str) -> bool:
        return channel in self.state.tripped

    def record_send(self, channel: str, ok: bool, on_date: date) -> bool:
        """Record one send result. Returns True if this call tripped the breaker."""
        if ok:
            self.state.consecutive_failures[channel] = 0
            key = on_date.isoformat()
            self.state.sends_by_date[key] = self.state.sends_by_date.get(key, 0) + 1
            return False

        n = self.state.consecutive_failures.get(channel, 0) + 1
        self.state.consecutive_failures[channel] = n
        threshold = self.cfg.breaker["consecutive_send_failures_to_pause"]
        if n >= threshold and channel not in self.state.tripped:
            self.state.tripped[channel] = datetime.now().isoformat(timespec="seconds")
            return True
        return False

    def sends_on(self, on_date: date) -> int:
        return self.state.sends_by_date.get(on_date.isoformat(), 0)

    def reset(self, channel: str | None = None) -> None:
        """Manual reset after a person confirms the channel is back. Deliberately no auto-recovery: a tripped breaker should get human eyes."""
        if channel:
            self.state.tripped.pop(channel, None)
            self.state.consecutive_failures[channel] = 0
        else:
            self.state.tripped.clear()
            self.state.consecutive_failures.clear()
        self.save()
