"""Customer reply parsing (product spec §4.2: "any keyword about changing payment terms escalates automatically").

**No LLM here, on purpose.**

From the spec: "the rule does not rely on the agent judging whether a request is reasonable; a matching keyword category escalates unconditionally."
Three reasons for plain rules:
  1. Auditable: the log shows exactly which word matched and which category it belongs to; no need to explain what a model was thinking;
  2. Cannot be bypassed: immune to prompt injection; a customer writing "ignore previous instructions" achieves nothing;
  3. Deterministic: the same message gives the same result every time, so a compliance review can reproduce it.

In this system the LLM **explains** (it writes the escalation summary for finance); it never **decides whether to escalate**.
The rule layer holds the decision and the model only explains it; that split is the key to the whole design.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class ReplyIntent(str, Enum):
    TERMS_CHANGE = "terms_change"   # instalments / discount / extension -> forced to high risk
    DISPUTE = "dispute"             # billing dispute / legal -> forced to high risk
    PAID_CLAIM = "paid_claim"       # says it has paid -> finance checks the payment, collection stops
    ACK = "ack"                     # acknowledged / promises to pay on time -> no escalation
    UNKNOWN = "unknown"

    @property
    def forces_escalation(self) -> bool:
        return self in (ReplyIntent.TERMS_CHANGE, ReplyIntent.DISPUTE)

    @property
    def label(self) -> str:
        return {
            "terms_change": "request to change payment terms",
            "dispute": "billing dispute / legal signal",
            "paid_claim": "customer says it has paid",
            "ack": "acknowledged / promises to pay",
            "unknown": "not recognised",
        }[self.value]


#: Keywords are grouped by category so finance and compliance staff can maintain them without a developer.
#: Entries starting with "re:" are regular expressions, used for phrasings with a variable such as "pay in 3 / 6 instalments".
#: Most entries are plain strings on purpose: finance staff can read them and add words without knowing regex.
KEYWORDS: dict[ReplyIntent, list[str]] = {
    ReplyIntent.TERMS_CHANGE: [
        "分期", "拆分付款", "分批付", "分几次",
        r"re:分\s*[0-9一二两三四五六七八九十几]+\s*期",     # e.g. 分三期 / 分6期 / 分几期 (in 3 / 6 / several instalments)
        r"re:([0-9]+|[一二两三四五六七八九十])\s*期\s*(付|支付|结清)",
        "打折", "折扣", "优惠", "减免", "少付", "抹零",
        "延期", "延后", "宽限", "缓一缓", "缓几天", "展期", "推迟", "晚点付", "下个月再付",
        "资金周转", "周转困难", "现金流紧张", "调整账期", "重新约定",
        "installment", "discount", "waive", "extend", "extension", "grace period",
    ],
    ReplyIntent.DISPUTE: [
        "对不上", "对账有问题", "金额不对", "货不对", "质量问题", "退货", "索赔",
        "不认可", "有异议", "争议", "律师", "法务", "起诉", "仲裁", "破产", "清算",
        "dispute", "incorrect amount", "legal", "lawyer", "bankrupt",
    ],
    ReplyIntent.PAID_CLAIM: [
        "已付", "已经付了", "已打款", "已转账", "已支付", "水单", "回单", "付款凭证",
        "already paid", "payment sent", "remitted",
    ],
    ReplyIntent.ACK: [
        "收到", "知道了", "好的", "会付", "安排付款", "本周付", "马上付",
        "noted", "will pay", "processing",
    ],
}

#: Priority: a message can match several categories; the highest-risk one wins.
#: e.g. "we have paid part of it, can we pay the rest in instalments?" matches both PAID_CLAIM and TERMS_CHANGE,
#: and must be handled as TERMS_CHANGE (forced escalation), never let through because "paid" matched first.
PRIORITY = [
    ReplyIntent.TERMS_CHANGE,
    ReplyIntent.DISPUTE,
    ReplyIntent.PAID_CLAIM,
    ReplyIntent.ACK,
]


@dataclass
class ReplyAnalysis:
    intent: ReplyIntent
    matched: list[str] = field(default_factory=list)
    all_intents: dict[str, list[str]] = field(default_factory=dict)
    raw: str = ""

    @property
    def forces_escalation(self) -> bool:
        return self.intent.forces_escalation

    def summary(self) -> str:
        if self.intent is ReplyIntent.UNKNOWN:
            return "no keyword category matched"
        return f"{self.intent.label} (matched: {', '.join(self.matched)})"


def _normalize(text: str) -> str:
    """Normalise the text so that extra spaces or full-width characters cannot slip past the keyword list.

    Variants such as "分 期" / "分－期" / "ＦＥＮ" really occur in collections,
    not always on purpose; an input method or a copy-paste can produce them. Clean first, then match.
    """
    text = text or ""
    # full-width to half-width
    text = "".join(
        chr(ord(ch) - 0xFEE0) if 0xFF01 <= ord(ch) <= 0xFF5E else ch for ch in text
    )
    text = text.replace("　", " ")
    # remove whitespace and common separators between CJK characters (spaces between English words are kept)
    text = re.sub(r"(?<=[一-鿿])[\s\-_·.、,，]+(?=[一-鿿])", "", text)
    return text.lower()


def classify_reply(text: str) -> ReplyAnalysis:
    """Assign a customer reply to an intent category. A match is a fact; no judgement of whether the request is reasonable."""
    norm = _normalize(text)
    hits: dict[ReplyIntent, list[str]] = {}

    for intent, words in KEYWORDS.items():
        matched: list[str] = []
        for w in words:
            if w.startswith("re:"):
                m = re.search(w[3:], norm)
                if m:
                    matched.append(m.group(0))
            elif _normalize(w) in norm:
                matched.append(w)
        if matched:
            hits[intent] = matched

    for intent in PRIORITY:
        if intent in hits:
            return ReplyAnalysis(
                intent=intent,
                matched=hits[intent],
                all_intents={i.value: w for i, w in hits.items()},
                raw=text,
            )

    return ReplyAnalysis(intent=ReplyIntent.UNKNOWN, all_intents={}, raw=text)
