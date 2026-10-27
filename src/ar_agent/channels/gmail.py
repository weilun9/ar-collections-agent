"""Gmail —— 两种用途，所以有两个类。

    GmailCustomerChannel  → 对客户发正式催收函（中度分级）
    GmailInternalChannel  → 对财务负责人/CFO 发高风险告警与周报

同一个底层传输，却拆成两个类，是为了让"对外"和"对内"在类型上不可混用
（见 base.py 的设计说明）。共用逻辑放在 _GmailTransport 里，不重复。

真实接入用 SMTP + 应用专用密码（比 OAuth 少一大堆授权步骤，演示够用）：
    GMAIL_SMTP_HOST=smtp.gmail.com
    GMAIL_SMTP_PORT=587
    GMAIL_USER=ar-agent@yourdomain.com
    GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
"""
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage

from ..models import Message, SendResult


class _GmailTransport:
    """SMTP 发送逻辑，被对外/对内两个渠道类共用。"""

    @staticmethod
    def deliver(msg: Message, channel_name: str) -> SendResult:
        host = os.environ.get("GMAIL_SMTP_HOST", "smtp.gmail.com")
        port = int(os.environ.get("GMAIL_SMTP_PORT", "587"))
        user = os.environ["GMAIL_USER"]
        password = os.environ["GMAIL_APP_PASSWORD"]

        mail = EmailMessage()
        mail["From"] = user
        mail["To"] = msg.to
        mail["Subject"] = msg.subject or "(无主题)"
        mail.set_content(msg.body)

        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(mail)
        return SendResult(True, channel_name, msg.to, "live", "已发送")


# 延迟导入避免循环
from .base import CustomerChannel, InternalChannel  # noqa: E402


class GmailCustomerChannel(CustomerChannel):
    name = "gmail"

    def _send_live(self, msg: Message) -> SendResult:
        return _GmailTransport.deliver(msg, self.name)


class GmailInternalChannel(InternalChannel):
    name = "gmail_internal"

    def _send_live(self, msg: Message) -> SendResult:
        return _GmailTransport.deliver(msg, self.name)
