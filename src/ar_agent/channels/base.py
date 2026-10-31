"""Channel abstraction (product spec §3.2).

**Core design: customer-facing and internal channels are separate types.**

    CustomerChannel  -> WhatsApp / Gmail (to customers)   reaches customers
    InternalChannel  -> Slack / Gmail (to the team)       only reaches colleagues

From the spec: "even if the agent misjudges, the worst case is an extra internal notification, not a wrong message to a customer."
It is two base classes rather than one send() with a flag, because a flag can be set wrongly and a type cannot.
To message a customer, the orchestrator must explicitly obtain a CustomerChannel instance,
and the path to that instance is guarded by the policy gate (agent_may_contact_customer).

Every channel is dry-run by default: messages are written to data/outbox/ instead of being sent.
Real sending needs both config.channels.<name>.live=true **and** the environment variable AR_LIVE=1.
"""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

from ..models import Message, SendResult


class Channel(ABC):
    name: str = ""
    audience: str = ""          # customer / internal

    def __init__(self, live: bool, outbox_dir: Path):
        self.live = live
        self.outbox_dir = Path(outbox_dir)
        self.outbox_dir.mkdir(parents=True, exist_ok=True)

    def send(self, msg: Message) -> SendResult:
        if msg.audience != self.audience:
            # Safety net: refuse an internal template on a customer channel (or the reverse).
            return SendResult(
                ok=False, channel=self.name, to=msg.to, mode="blocked",
                detail=f"audience mismatch: template audience={msg.audience}, channel audience={self.audience}",
            )
        if self.live:
            try:
                return self._send_live(msg)
            except Exception as exc:                       # noqa: BLE001
                return SendResult(False, self.name, msg.to, "live", f"{type(exc).__name__}: {exc}")
        return self._send_dry(msg)

    def _send_dry(self, msg: Message) -> SendResult:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        safe_to = re.sub(r"[^A-Za-z0-9._@+-]", "_", msg.to)[:40]
        path = self.outbox_dir / f"{ts}_{self.name}_{safe_to}.json"
        payload = {
            "channel": self.name,
            "audience": self.audience,
            "to": msg.to,
            "subject": msg.subject,
            "body": msg.body,
            "template_id": msg.template_id,
            "invoice_id": msg.invoice_id,
            "customer_id": msg.customer_id,
            "sent_at": datetime.now().isoformat(timespec="seconds"),
            "mode": "dry_run",
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        return SendResult(True, self.name, msg.to, "dry_run", "written to outbox (not actually sent)", path.name)

    @abstractmethod
    def _send_live(self, msg: Message) -> SendResult: ...


class CustomerChannel(Channel):
    """Customer-facing channel: reaches real customers. Every call must pass the policy gate first."""
    audience = "customer"


class InternalChannel(Channel):
    """Internal channel: only notifies colleagues. The cost of a false alarm is a colleague taking a look, which is acceptable."""
    audience = "internal"
