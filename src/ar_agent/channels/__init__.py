from .base import Channel, CustomerChannel, InternalChannel
from .gmail import GmailCustomerChannel, GmailInternalChannel
from .slack import SlackChannel
from .whatsapp import WhatsAppChannel

__all__ = [
    "Channel", "CustomerChannel", "InternalChannel",
    "WhatsAppChannel", "GmailCustomerChannel", "GmailInternalChannel", "SlackChannel",
]


def build_registry(cfg, outbox_dir):
    """Build the channels from config. Everything is dry-run by default."""
    return {
        "whatsapp": WhatsAppChannel(cfg.channel_is_live("whatsapp"), outbox_dir),
        "gmail": GmailCustomerChannel(cfg.channel_is_live("gmail"), outbox_dir),
        "gmail_internal": GmailInternalChannel(cfg.channel_is_live("gmail"), outbox_dir),
        "slack": SlackChannel(cfg.channel_is_live("slack"), outbox_dir),
    }
