"""WhatsApp Business（Meta Cloud API）—— 对客户发送轻度提醒。

真实接入需要 .env：
    WHATSAPP_PHONE_NUMBER_ID=...
    WHATSAPP_TOKEN=...

生产注意（面试可讲的一个真实约束）：
Meta 规定商家只能在客户 24 小时会话窗口内自由发文本；窗口外必须用**预审批模板**
（Message Template，需 Meta 审核通过）。这跟我们 L2 的"只发预设模板"约束刚好同向 ——
平台的合规要求和我们的风险设计是一致的，不是额外负担。
下面 live 实现按模板消息发送；窗口内的纯文本走 type=text 分支。
"""
from __future__ import annotations

import json
import os
import urllib.request

from ..models import Message, SendResult
from .base import CustomerChannel

GRAPH_API = "https://graph.facebook.com/v21.0"


class WhatsAppChannel(CustomerChannel):
    name = "whatsapp"

    def _send_live(self, msg: Message) -> SendResult:
        phone_id = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
        token = os.environ["WHATSAPP_TOKEN"]

        payload = {
            "messaging_product": "whatsapp",
            "to": msg.to,
            "type": "text",
            "text": {"preview_url": False, "body": msg.body},
        }
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
