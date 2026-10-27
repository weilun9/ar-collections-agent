"""L4 运行时策略层 —— PM 文档 §5 的核心防线。

这一层的设计要点：

    **它跑在 LLM 之外，且跑在分级引擎之后。**

system prompt 里写"任何还款条件变更必须转人工"是**软约束** —— 模型可能忽略、
可能被 prompt injection 绕过、可能在长上下文里遗忘。而这一层是 Python 代码：
它不读 prompt、不调模型，只看事实与阈值，命中即执行。Agent 没有任何办法说服它。

执行顺序：分级引擎给出 base_tier → 策略链逐条过 → 得到最终 tier 与是否允许对外发送。
每条规则只能让结果**更严格**（升级、禁止对外发送），不能放宽。这个单调性是刻意的：
新增规则永远不会意外打开一个之前关着的口子。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date

from .config import Config
from .models import Customer, Invoice, Tier


@dataclass
class PolicyContext:
    """策略判定所需的全部输入。"""
    invoice: Invoice
    customer: Customer
    base_tier: Tier
    today: date
    sends_today: int = 0
    inbound_intent: str | None = None      # 来自 inbound.classify_reply
    inbound_summary: str = ""


@dataclass
class PolicyOutcome:
    tier: Tier
    agent_may_contact_customer: bool = True
    hits: list[str] = field(default_factory=list)
    halt_all_sending: bool = False         # 全局暂停（日发送量熔断）

    def escalate_to(self, tier: Tier, reason: str) -> None:
        """只升不降：策略层永远不会把一条账款判得更轻。"""
        if tier.rank > self.tier.rank:
            self.tier = tier
        self.hits.append(reason)

    def forbid_customer_contact(self, reason: str) -> None:
        self.agent_may_contact_customer = False
        if reason not in self.hits:
            self.hits.append(reason)


class PolicyRule(ABC):
    id: str = ""
    description: str = ""

    @abstractmethod
    def apply(self, ctx: PolicyContext, out: PolicyOutcome, cfg: Config) -> None: ...


# --------------------------------------------------------------------------- #
# 规则实现
# --------------------------------------------------------------------------- #

class TermsChangeForcesHigh(PolicyRule):
    """客户提出还款条件变更 → 无条件升级高风险，不论当前分级。

    PM 文档 §4.2。这条是"复杂任务逻辑不能被绕过"的具体落地：
    判定依据来自 inbound.py 的纯关键词匹配，不是模型的语义判断。
    """
    id = "P-01"
    description = "还款条件变更诉求（分期/打折/延期）强制升级高风险"

    def apply(self, ctx, out, cfg):
        if not cfg.policy.get("terms_change_forces_high", True):
            return
        if ctx.inbound_intent == "terms_change":
            out.escalate_to(Tier.HIGH, f"{self.id} 还款条件变更诉求 → 强制高风险（{ctx.inbound_summary}）")
            out.forbid_customer_contact(f"{self.id} 条件变更须人工决策，Agent 停止一切对外动作")


class DisputeForcesHigh(PolicyRule):
    """账目争议 / 法务信号 → 强制升级高风险。"""
    id = "P-02"
    description = "账目争议或法务信号强制升级高风险"

    def apply(self, ctx, out, cfg):
        if not cfg.policy.get("dispute_forces_high", True):
            return
        if ctx.inbound_intent == "dispute":
            out.escalate_to(Tier.HIGH, f"{self.id} 账目争议/法务信号 → 强制高风险（{ctx.inbound_summary}）")
            out.forbid_customer_contact(f"{self.id} 争议未澄清前 Agent 不得继续催收")


class HardAmountCeiling(PolicyRule):
    """超过金额红线的账款，无论分级，Agent 都不能自动处理。

    PM 文档 §5 L4 原话：「任何超过 ¥200,000 的账款 → 无论分级都不能被 Agent 自动处理」。
    注意它与分级引擎里的高风险金额阈值是**两条独立的规则**，只是当前配置值相同。
    分开写不是冗余：分级阈值是给人看的风险标签，这条是给机器执行的动作闸门。
    哪天业务想把"高风险标签线"降到 15 万但"自动化禁止线"仍保持 20 万，改配置即可，
    不需要动代码 —— 标签与权限本来就该解耦。
    """
    id = "P-03"
    description = "金额超过红线的账款禁止 Agent 自动对外处理"

    def apply(self, ctx, out, cfg):
        ceiling = cfg.policy["hard_human_only_amount"]
        if ctx.invoice.amount > ceiling:
            out.escalate_to(Tier.HIGH, f"{self.id} 金额 ¥{ctx.invoice.amount:,.0f} > 红线 ¥{ceiling:,.0f}")
            out.forbid_customer_contact(f"{self.id} 超红线账款仅限人工处理")


class VipAlwaysHuman(PolicyRule):
    """VIP 客户默认强制转人工。

    PM 文档 §6.2 反向指标：「VIP 客户被 Agent 自动催收的次数，目标 0」。
    指标定成 0，就不能只靠"希望它不发生"，得有一条规则在运行时保证它不可能发生。
    这就是反向指标与策略层的对应关系 —— 每个 guardrail metric 都该有一条规则托底。
    """
    id = "P-04"
    description = "VIP 客户禁止 Agent 自动催收，一律转人工"

    def apply(self, ctx, out, cfg):
        if cfg.policy.get("vip_always_human", True) and ctx.customer.is_vip:
            out.escalate_to(Tier.HIGH, f"{self.id} VIP 客户 {ctx.customer.name} → 强制人工")
            out.forbid_customer_contact(f"{self.id} VIP 客户不接受自动催收")


class DailySendCap(PolicyRule):
    """单日发送量超限 → 暂停并通知财务。

    这是防"逻辑写错导致群发轰炸"的兜底。哪怕分级引擎彻底判错，
    单日也最多打扰 N 个客户，损失有上界。
    """
    id = "P-05"
    description = "单日对外发送量超过上限时全局暂停"

    def apply(self, ctx, out, cfg):
        cap = cfg.policy["daily_send_cap"]
        if ctx.sends_today >= cap:
            out.halt_all_sending = True
            out.forbid_customer_contact(f"{self.id} 单日发送量已达上限 {cap} 条，暂停并通知财务")


class PaidClaimPauses(PolicyRule):
    """客户声称已付款 → 暂停催收，转财务核账。

    文档没有明确写这条，但它是必要的：在财务确认到账前继续催一个已经付了款的客户，
    是最伤客户关系的一类错误，而且完全可以避免。做法与"争议"一致 —— 停止自动动作、转人工核实。
    """
    id = "P-06"
    description = "客户声称已付款时暂停自动催收，转财务核账"

    def apply(self, ctx, out, cfg):
        if ctx.inbound_intent == "paid_claim":
            out.forbid_customer_contact(f"{self.id} 客户称已付款，待财务核账前停止催收")


#: 策略链。顺序无关（每条规则只做单调收紧），但按编号排列便于日志阅读与审计。
DEFAULT_RULES: list[PolicyRule] = [
    TermsChangeForcesHigh(),
    DisputeForcesHigh(),
    HardAmountCeiling(),
    VipAlwaysHuman(),
    DailySendCap(),
    PaidClaimPauses(),
]


def evaluate(ctx: PolicyContext, cfg: Config, rules: list[PolicyRule] | None = None) -> PolicyOutcome:
    """跑完整条策略链，返回最终裁决。"""
    out = PolicyOutcome(tier=ctx.base_tier)
    for rule in (rules if rules is not None else DEFAULT_RULES):
        rule.apply(ctx, out, cfg)
    return out


def rule_catalog() -> list[dict[str, str]]:
    """给 Dashboard / 审计报告用的规则清单。"""
    return [{"id": r.id, "description": r.description} for r in DEFAULT_RULES]
