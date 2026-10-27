"""L2 工具设计 —— PM 文档 §5：「只能发送预设模板，不能自由生成还款承诺类文本」。

落地方式有两层，缺一不可：

  **第一层：接口层面根本不给自由文本的入口。**
  render() 的签名是 (template_id, 变量字典)，没有 body/text/content 参数。
  Agent 能选模板、能填变量（金额、日期、单号），但无法凭空造一句
  "我们同意您分三期支付"。这不是"我们要求它别这么做"，是**它做不到**。

  **第二层：渲染完再扫一遍禁用词。**
  万一模板本身被人误改、或变量注入带进了危险措辞，发送前还有一道 guard。
  纵深防御 —— 假设第一层会被绕过，再问一次"就算绕过了，最坏会发出什么"。

话术设计原则：专业、克制、信息完整（金额/到期日/付款链接），不用施压性措辞
（对应 §一 用户故事"不会因为一次自动化提醒而对合作关系产生负面感受"、§5 L7 合规）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass


class TemplateViolation(Exception):
    """渲染结果命中禁用措辞时抛出，发送流程中止。"""


@dataclass(frozen=True)
class Template:
    id: str
    channel: str
    audience: str          # customer / internal
    subject: str
    body: str
    note: str = ""


#: 禁用措辞：任何暗示"改变还款条件"或"施压"的表达。
#: 前者对应 §一 Non-Goals（改变还款条件必须人工决策）；
#: 后者对应 §5 L7（催收内容需符合当地催收行为合规要求，禁止施压性话术）。
FORBIDDEN_PATTERNS: list[tuple[str, str]] = [
    (r"分\s*期|installment", "不得承诺分期"),
    (r"打\s*折|折扣|减免|优惠|discount|waive", "不得承诺打折减免"),
    (r"延\s*期|延\s*后|宽\s*限|展\s*期", "不得承诺延期"),
    (r"免除|豁免|一笔勾销", "不得承诺免除债务"),
    (r"起诉|上门|曝光|黑名单|失信|后果自负|严重后果", "不得使用施压/威胁性话术"),
    (r"立即|马上|务必|限你", "不得使用胁迫性时限措辞"),
]

_CODE_FENCE = "```"

TEMPLATES: dict[str, Template] = {
    # ---------------- 对客户（轻度：WhatsApp 友好提醒）----------------
    "WA_LOW_1": Template(
        id="WA_LOW_1",
        channel="whatsapp",
        audience="customer",
        subject="",
        body=(
            "{contact_name} 您好，我是{company}的财务助手。\n\n"
            "温馨提醒：发票 {invoice_id}（金额 ¥{amount}）的付款日为 {due_date}，"
            "目前显示尚未收到款项。\n\n"
            "如已完成付款，请忽略本条消息，或把付款凭证发给我们核对即可。\n"
            "付款链接：{payment_url}\n\n"
            "如对账目有任何疑问，回复本条消息即可，会有同事跟进。感谢您的配合。"
        ),
        note="第 1 次友好提醒，语气轻，明确给出「已付请忽略」的出口",
    ),
    "WA_LOW_2": Template(
        id="WA_LOW_2",
        channel="whatsapp",
        audience="customer",
        subject="",
        body=(
            "{contact_name} 您好，关于发票 {invoice_id}（金额 ¥{amount}，付款日 {due_date}），"
            "系统显示仍未收到款项，再次与您确认一下。\n\n"
            "付款链接：{payment_url}\n\n"
            "如付款安排上需要沟通，或账目需要核对，回复本条消息，"
            "我们会安排对接同事直接与您联系。"
        ),
        note="第 2 次提醒，仍不加压；「需要沟通」的措辞是有意的——引导客户说出诉求，触发关键词升级转人工",
    ),

    # ---------------- 对客户（中度：Gmail 正式催收函）----------------
    "MAIL_MID_DUNNING": Template(
        id="MAIL_MID_DUNNING",
        channel="gmail",
        audience="customer",
        subject="付款提醒 · 发票 {invoice_id} · {company}",
        body=(
            "{contact_name} 您好，\n\n"
            "现就以下应收款项与贵司确认：\n\n"
            "    发票编号：{invoice_id}\n"
            "    金额：¥{amount}\n"
            "    开票日期：{issue_date}\n"
            "    约定付款日：{due_date}\n"
            "    逾期天数：{days_overdue} 天\n\n"
            "截至本函发出时，我方尚未收到上述款项。烦请贵司财务同事协助核实付款状态。\n\n"
            "付款链接：{payment_url}\n\n"
            "若款项已在处理中、或贵司对账目存在疑问，请直接回复本邮件说明情况，"
            "我方会安排对应同事跟进核对。\n\n"
            "感谢贵司一直以来的合作。\n\n"
            "{company} 财务部\n"
            "（本邮件由应收账款系统按既定流程发出，仅作对账提醒之用）"
        ),
        note="正式但克制。对应 §八 Q2：客户经理未响应时发这封，不是加压话术",
    ),

    # ---------------- 对内（Slack：客户经理缓冲期通知）----------------
    "SLACK_AM_BUFFER": Template(
        id="SLACK_AM_BUFFER",
        channel="slack",
        audience="internal",
        subject="",
        body=(
            ":large_yellow_circle: *中风险账款 · 需要你决定是否先介入*\n\n"
            "• 客户：*{customer_name}*（客户经理：{account_manager}）\n"
            "• 发票：{invoice_id}　金额：¥{amount}\n"
            "• 付款日：{due_date}　已逾期：*{days_overdue} 天*\n"
            "• 分级依据：{reasons}\n\n"
            "*你有 {buffer_hours} 小时的优先处理权*（截止 {buffer_until}）。\n"
            "在此之前你可以自己联系客户；如需接管，执行：\n"
            + _CODE_FENCE + "\nar takeover {invoice_id} --by {account_manager}\n" + _CODE_FENCE + "\n"
            "若到期仍未响应，系统将按标准模板发出正式对账邮件，"
            "并在周报中记录本条为「客户经理未响应」。"
        ),
        note="缓冲期通知。明确写出「到期会怎样」，让客户经理知道不响应的后果，而不是被系统偷偷接管",
    ),

    # ---------------- 对内（Slack：高风险告警）----------------
    "SLACK_HIGH_ALERT": Template(
        id="SLACK_HIGH_ALERT",
        channel="slack",
        audience="internal",
        subject="",
        body=(
            ":red_circle: *高风险账款 · 需人工决策*\n\n"
            "• 客户：*{customer_name}*（客户经理：{account_manager}）\n"
            "• 发票：{invoice_id}　金额：*¥{amount}*\n"
            "• 付款日：{due_date}　已逾期：*{days_overdue} 天*\n"
            "• 升级原因：{reasons}\n"
            "• 命中策略：{policy_hits}\n\n"
            "*系统未向该客户发送任何消息，也不会自动发送。*\n"
            "历史沟通摘要：{history}\n\n"
            "请人工决定下一步：继续催收 / 启动法务 / 协商还款计划。"
        ),
        note="高风险只通知不外发。「系统未发送任何消息」这句是刻意加粗的——让财务一眼确认客户没被打扰",
    ),

    # ---------------- 对内（Gmail：财务负责人高风险书面通知）----------------
    "MAIL_FINANCE_ALERT": Template(
        id="MAIL_FINANCE_ALERT",
        channel="gmail",
        audience="internal",
        subject="[高风险应收] {customer_name} · {invoice_id} · ¥{amount}",
        body=(
            "以下账款已被系统标记为高风险，需人工决策：\n\n"
            "    客户：{customer_name}\n"
            "    客户经理：{account_manager}\n"
            "    发票编号：{invoice_id}\n"
            "    金额：¥{amount}\n"
            "    约定付款日：{due_date}\n"
            "    逾期天数：{days_overdue} 天\n\n"
            "升级原因：\n{reasons}\n\n"
            "命中的运行时策略：\n{policy_hits}\n\n"
            "历史沟通摘要：\n{history}\n\n"
            "系统未向该客户发送任何自动消息，后续动作需人工发起。\n"
        ),
        note="给财务负责人的书面留痕，与 Slack 告警并行——Slack 会被刷走，邮件不会",
    ),

    # ---------------- 对内（Slack：熔断/策略暂停告警）----------------
    "SLACK_BREAKER": Template(
        id="SLACK_BREAKER",
        channel="slack",
        audience="internal",
        subject="",
        body=(
            ":rotating_light: *应收催收 Agent 已暂停*\n\n"
            "• 触发原因：{reason}\n"
            "• 时间：{timestamp}\n"
            "• 受影响渠道：{channel}\n\n"
            "系统已停止对外发送。排查并确认渠道恢复后执行：\n"
            + _CODE_FENCE + "\nar reset-breaker --channel {channel}\n" + _CODE_FENCE + "\n"
            "（刻意不做自动恢复：熔断意味着有事情不对劲，需要有人看一眼。）"
        ),
        note="熔断告警",
    ),
}


def _guard(text: str, template_id: str) -> None:
    """发送前的内容闸门（纵深防御第二层）。"""
    for pattern, why in FORBIDDEN_PATTERNS:
        m = re.search(pattern, text)
        if m:
            raise TemplateViolation(
                f"模板 {template_id} 渲染结果命中禁用措辞「{m.group(0)}」：{why}"
            )


def render(template_id: str, variables: dict) -> tuple[str, str]:
    """渲染模板，返回 (subject, body)。

    注意签名：**没有任何自由文本参数**。Agent 只能选 template_id 并提供结构化变量。
    这是 L2 的关键 —— 能力边界写在接口里，而不是写在 prompt 里。
    """
    tpl = TEMPLATES.get(template_id)
    if tpl is None:
        raise KeyError(
            f"未知模板: {template_id}（可用模板由财务/合规审核后加入，Agent 不能自行创建）"
        )

    safe = {k: ("" if v is None else str(v)) for k, v in variables.items()}
    try:
        subject = tpl.subject.format(**safe)
        body = tpl.body.format(**safe)
    except KeyError as exc:
        raise KeyError(f"模板 {template_id} 缺少变量: {exc}") from exc

    if tpl.audience == "customer":
        # 只对"发给客户"的内容做禁用词校验：内部告警里出现"分期""起诉"是正常的，
        # 财务本来就需要看到这些词才能决策。对内对外用不同尺度，是有意为之。
        _guard(body, template_id)
        _guard(subject, template_id)

    return subject, body


def catalog() -> list[dict]:
    return [
        {"id": t.id, "channel": t.channel, "audience": t.audience, "note": t.note}
        for t in TEMPLATES.values()
    ]
