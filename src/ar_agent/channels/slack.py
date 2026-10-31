"""Slack: an internal alert channel only.

Product spec §3.2: Slack is "write-only (notifications), no automated decisions".
So this class **can only send, never read**: the agent never reads what colleagues say in Slack,
let alone treats a chat message as an instruction. To take over a case, a person uses the explicit CLI command (ar takeover),
not a Slack reply such as "I'll handle it" that silently changes the agent's state.

This is deliberate: chat history is **context**, not **authorisation**. Treating it as authorisation
would give anyone who can post in that channel a way to operate the system.

Live sending uses an Incoming Webhook (simplest, no bot scope needed):
    SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
or a Bot Token to post to a given channel or DM:
    SLACK_BOT_TOKEN=xoxb-...
"""
from __future__ import annotations

import json
import os
import urllib.request

from ..models import Message, SendResult
from .base import InternalChannel


class SlackChannel(InternalChannel):
    name = "slack"

    def _send_live(self, msg: Message) -> SendResult:
        token = os.environ.get("SLACK_BOT_TOKEN", "")
        if token:
            payload = {"channel": msg.to, "text": msg.body, "mrkdwn": True}
            req = urllib.request.Request(
                "https://slack.com/api/chat.postMessage",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            if not body.get("ok"):
                return SendResult(False, self.name, msg.to, "live", str(body.get("error")))
            return SendResult(True, self.name, msg.to, "live", "sent", body.get("ts", ""))

        webhook = os.environ["SLACK_WEBHOOK_URL"]
        req = urllib.request.Request(
            webhook,
            data=json.dumps({"text": msg.body}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
        return SendResult(True, self.name, msg.to, "live", "sent (webhook)")
