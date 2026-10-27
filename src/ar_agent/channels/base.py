"""渠道抽象层 —— PM 文档 §3.2。

**核心设计：对外渠道与对内渠道在类型系统层面就是分开的。**

    CustomerChannel  → WhatsApp / Gmail(对客户)   会打扰到客户
    InternalChannel  → Slack / Gmail(对团队)      只打扰同事

文档原话：「即使 Agent 误判，最坏情况也是'内部多发了一条通知'，而不是'对客户发错了消息'」。
把它做成两个基类而不是一个 send() 加个 flag，是因为 flag 会写错，类型不会。
编排层想对客户发消息，必须显式拿到一个 CustomerChannel 实例，
而拿这个实例的路径上有 policy 闸门守着（agent_may_contact_customer）。

所有渠道默认 dry-run：消息写入 data/outbox/ 而不真的发出。
要真的发出去，必须同时满足 config.channels.<name>.live=true **且** 环境变量 AR_LIVE=1。
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
            # 防呆：把对内模板塞进对外渠道（或反过来）直接拒绝。
            return SendResult(
                ok=False, channel=self.name, to=msg.to, mode="blocked",
                detail=f"受众不匹配：模板 audience={msg.audience}，渠道 audience={self.audience}",
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
        return SendResult(True, self.name, msg.to, "dry_run", "已写入 outbox（未真实发送）", path.name)

    @abstractmethod
    def _send_live(self, msg: Message) -> SendResult: ...


class CustomerChannel(Channel):
    """对外渠道：会真正打扰到客户。任何调用都必须先过 policy 闸门。"""
    audience = "customer"


class InternalChannel(Channel):
    """对内渠道：只通知同事。误报的代价是同事多看一眼，可接受。"""
    audience = "internal"
