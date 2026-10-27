"""WhatsApp Business（Meta Cloud API）—— 对客户发送轻度提醒。

真实接入需要 .env：
    WHATSAPP_PHONE_NUMBER_ID=...
    WHATSAPP_TOKEN=...

平台约束：Meta 只允许在客户 24 小时会话窗口内发送自由文本；窗口外必须使用预审批模板
（Message Template）。催收提醒是主动发起的，基本都在窗口外，因此 live 发送一律走
type=template，正文由 Meta 端审批过的模板决定，本地只传变量。
这与 L2「只发预设模板」的约束一致：本地模板没有对应的 Meta 模板时直接拒绝发送（fail closed），
不会退回自由文本。
"""
from __future__ import annotations

import json
import os
import urllib.request

from ..models import Message, SendResult
from ..templates import WHATSAPP_META_TEMPLATES
from .base import CustomerChannel

GRAPH_API = "https://graph.facebook.com/v21.0"


class UnmappedTemplate(Exception):
    """本地模板没有对应的 Meta 审批模板，拒绝发送。"""


def build_payload(msg: Message) -> dict:
    """把本地模板消息转换成 Meta 模板消息请求体。纯函数，便于测试。"""
    mapping = WHATSAPP_META_TEMPLATES.get(msg.template_id)
    if mapping is None:
        raise UnmappedTemplate(f"模板 {msg.template_id} 没有对应的 WhatsApp 审批模板")
    name, lang, param_names = mapping
    missing = [p for p in param_names if p not in msg.variables]
    if missing:
        raise UnmappedTemplate(f"模板 {msg.template_id} 缺少变量: {', '.join(missing)}")
    return {
        "messaging_product": "whatsapp",
        "to": msg.to,
        "type": "template",
        "template": {
            "name": name,
            "language": {"code": lang},
            "components": [{
                "type": "body",
                "parameters": [{"type": "text", "text": str(msg.variables[p])} for p in param_names],
            }],
        },
    }


class WhatsAppChannel(CustomerChannel):
    name = "whatsapp"

    def _send_live(self, msg: Message) -> SendResult:
        payload = build_payload(msg)          # 无映射时抛异常，由 Channel.send 记为发送失败
        phone_id = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
        token = os.environ["WHATSAPP_TOKEN"]
        req = urllib.request.Request(
            f"{GRAPH_API}/{phone_id}/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        ref = (body.get("messages") or [{}])[0].get("id", "")
        return SendResult(True, self.name, msg.to, "live", "已发送", ref)
