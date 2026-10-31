"""Gmail: two uses, so two classes.

    GmailCustomerChannel  -> formal dunning letters to customers (medium tier)
    GmailInternalChannel  -> high-risk alerts and the weekly report to the finance lead / CFO

One transport, split into two classes so that customer-facing and internal sending cannot be mixed up by type
(see the design note in base.py). The shared logic lives in _GmailTransport.

Live sending uses SMTP with an app password (far fewer authorisation steps than OAuth):
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
    """SMTP sending, shared by the customer-facing and internal channel classes."""

    @staticmethod
    def deliver(msg: Message, channel_name: str) -> SendResult:
        host = os.environ.get("GMAIL_SMTP_HOST", "smtp.gmail.com")
        port = int(os.environ.get("GMAIL_SMTP_PORT", "587"))
        user = os.environ["GMAIL_USER"]
        password = os.environ["GMAIL_APP_PASSWORD"]

        mail = EmailMessage()
        mail["From"] = user
        mail["To"] = msg.to
        mail["Subject"] = msg.subject or "(no subject)"
        mail.set_content(msg.body)

        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(mail)
        return SendResult(True, channel_name, msg.to, "live", "sent")


# Imported late to avoid a circular import
from .base import CustomerChannel, InternalChannel  # noqa: E402


class GmailCustomerChannel(CustomerChannel):
    name = "gmail"

    def _send_live(self, msg: Message) -> SendResult:
        return _GmailTransport.deliver(msg, self.name)


class GmailInternalChannel(InternalChannel):
    name = "gmail_internal"

    def _send_live(self, msg: Message) -> SendResult:
        return _GmailTransport.deliver(msg, self.name)
