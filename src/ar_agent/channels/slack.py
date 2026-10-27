"""Slack —— 纯对内告警渠道。

PM 文档 §3.2 明确：Slack「只写（通知），不做自动化决策」。
所以这里**只有发送能力，没有读取能力** —— Agent 不会去读 Slack 里同事说了什么，
更不会把同事的一句话当成指令去执行。人要接管，走 CLI 的显式命令（ar takeover），
而不是在 Slack 里回一句"我来处理"就让 Agent 自动改状态。

这是有意的：聊天记录是**上下文**，不是**授权**。把它当授权用，
等于给任何能在那个频道说话的人开了一个操作系统的口子。

真实接入用 Incoming Webhook（最简单，无需 bot scope）：
    SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
或用 Bot Token 发到指定频道/私聊：
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
            return SendResult(True, self.name, msg.to, "live", "已发送", body.get("ts", ""))

        webhook = os.environ["SLACK_WEBHOOK_URL"]
        req = urllib.request.Request(
            webhook,
            data=json.dumps({"text": msg.body}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
        return SendResult(True, self.name, msg.to, "live", "已发送(webhook)")
