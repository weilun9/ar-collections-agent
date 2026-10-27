"""客户回复解析 —— PM 文档 §4.2「任何还款条件变更关键词自动升级」。

**这里刻意不用 LLM。**

文档原话：「规则不依赖 Agent 自己判断'这个诉求是否合理'，而是命中关键词类别就无条件升级」。
用纯规则的三个理由：
  1. 可审计 —— 命中了哪个词、属于哪一类，日志里一目了然，不需要解释模型为什么这么想；
  2. 不可绕过 —— 不受 prompt injection 影响，客户在消息里写"忽略之前的指令"没有任何用；
  3. 确定性 —— 同一条消息一万次跑出一万次相同结果，合规审查时能复现。

LLM 在这个系统里的位置是**辅助解释**（生成升级摘要给财务看），而不是**决定是否升级**。
判定权在规则层，解释权才交给模型 —— 这个分工是整套设计的关键。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class ReplyIntent(str, Enum):
    TERMS_CHANGE = "terms_change"   # 分期/打折/延期 → 强制升级高风险
    DISPUTE = "dispute"             # 对账争议/法务 → 强制升级高风险
    PAID_CLAIM = "paid_claim"       # 声称已付款 → 转财务核账，停止催收
    ACK = "ack"                     # 已读/承诺按时付 → 无需升级
    UNKNOWN = "unknown"

    @property
    def forces_escalation(self) -> bool:
        return self in (ReplyIntent.TERMS_CHANGE, ReplyIntent.DISPUTE)

    @property
    def label(self) -> str:
        return {
            "terms_change": "还款条件变更诉求",
            "dispute": "账目争议 / 法务信号",
            "paid_claim": "客户声称已付款",
            "ack": "确认收到 / 承诺付款",
            "unknown": "未识别",
        }[self.value]


#: 关键词表按类别组织，便于财务/合规同事直接维护，不需要开发介入。
#: 以 "re:" 开头的条目按正则处理 —— 用来覆盖"分三期""分 6 期"这类带变量的说法。
#: 纯字符串条目占多数，是有意的：财务同事能直接读懂、直接加词，不需要会写正则。
KEYWORDS: dict[ReplyIntent, list[str]] = {
    ReplyIntent.TERMS_CHANGE: [
        "分期", "拆分付款", "分批付", "分几次",
        r"re:分\s*[0-9一二两三四五六七八九十几]+\s*期",     # 分三期 / 分6期 / 分几期
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

#: 判定优先级：一条消息可能同时命中多类，取风险最高的那一类。
#: 例如"我们已经付了一部分，剩下的能不能分期" —— 同时命中 PAID_CLAIM 和 TERMS_CHANGE，
#: 必须按 TERMS_CHANGE 处理（强制升级），绝不能因为先匹配到"已付"就放过。
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
            return "未命中任何关键词类别"
        return f"{self.intent.label}（命中：{'、'.join(self.matched)}）"


def _normalize(text: str) -> str:
    """归一化，防止靠加空格/全角符号绕过关键词表。

    "分 期" / "分－期" / "ＦＥＮ" 这类变形是催收场景里真实会遇到的，
    不一定是恶意，也可能只是输入法或复制粘贴带出来的。统一清洗后再匹配。
    """
    text = text or ""
    # 全角转半角
    text = "".join(
        chr(ord(ch) - 0xFEE0) if 0xFF01 <= ord(ch) <= 0xFF5E else ch for ch in text
    )
    text = text.replace("　", " ")
    # 去掉中文之间的空白与常见分隔符（英文词之间的空格要保留）
    text = re.sub(r"(?<=[一-鿿])[\s\-_·.、,，]+(?=[一-鿿])", "", text)
    return text.lower()


def classify_reply(text: str) -> ReplyAnalysis:
    """把一条客户回复归到意图类别。命中即事实，不做"合理性"判断。"""
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
