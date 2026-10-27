"""生成演示数据。

数据是**按分支设计的**，不是随机造的 —— 30 条账款刻意覆盖了决策矩阵的每一格
和 L4 策略层的每一条规则，这样演示时可以现场指着任意一条问"这条为什么这么判"。

日期全部相对锚点日生成，所以任何时候重新 seed，账龄结构都一样，
演示不会因为"数据放了两周变味了"而失真。
"""
from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
from pathlib import Path

CUSTOMER_COLUMNS = [
    "customer_id", "name", "contact_name", "whatsapp", "email",
    "account_manager", "am_slack_id", "is_vip", "prior_late_count",
    "avg_days_to_pay", "credit_terms_days",
]

from .store.csv_store import INVOICE_COLUMNS  # noqa: E402
from .orchestrator import INBOX_COLUMNS       # noqa: E402

CUSTOMERS = [
    # id     名称        联系人   WhatsApp        邮箱域                AM    slack        VIP 历史逾期 平均回款 账期
    ("C001", "恒信机电",  "张伟",  "+8613800000101", "zhang@hengxin.example",  "王磊", "@wanglei",  0, 0, 28, 30),
    ("C002", "蓝海科技",  "李静",  "+8613800000102", "lijing@lanhai.example",  "王磊", "@wanglei",  0, 0, 25, 30),
    ("C003", "宏图建材",  "赵强",  "+8613800000103", "zhaoq@hongtu.example",   "李娜", "@lina",     0, 1, 41, 45),
    ("C004", "中远物流",  "孙敏",  "+8613800000104", "sunmin@zhongyuan.example", "李娜", "@lina",   1, 0, 30, 45),
    ("C005", "佳诚电子",  "周涛",  "+8613800000105", "zhout@jiacheng.example", "陈晨", "@chenchen", 0, 0, 33, 30),
    ("C006", "三江食品",  "吴丽",  "+8613800000106", "wuli@sanjiang.example",  "陈晨", "@chenchen", 0, 2, 52, 60),
    ("C007", "天成纺织",  "郑凯",  "+8613800000107", "zhengk@tiancheng.example", "王磊", "@wanglei", 0, 0, 36, 30),
    ("C008", "瑞丰化工",  "冯洋",  "+8613800000108", "fengy@ruifeng.example",  "李娜", "@lina",     0, 0, 29, 30),
    ("C009", "金鼎五金",  "许倩",  "+8613800000109", "xuqian@jinding.example", "陈晨", "@chenchen", 0, 1, 47, 45),
    ("C010", "华通汽配",  "何军",  "+8613800000110", "hejun@huatong.example",  "王磊", "@wanglei",  1, 0, 27, 60),
    ("C011", "星宇包装",  "曹雪",  "+8613800000111", "caox@xingyu.example",    "李娜", "@lina",     0, 0, 31, 30),
    ("C012", "德茂仪器",  "潘勇",  "+8613800000112", "panyong@demao.example",  "陈晨", "@chenchen", 0, 3, 68, 45),
]

#: (发票号, 客户, 金额, 逾期天数[负数=未到期], 状态, 设计意图)
INVOICES = [
    # ---- 🟢 轻度：Agent 自主处理 ----
    ("INV-2001", "C001",  12000,   3, "open", "轻度：首次 WhatsApp 提醒"),
    ("INV-2002", "C002",   8500,   9, "open", "轻度：客户已回复 ack，不升级"),
    ("INV-2003", "C007",  23400,  12, "open", "轻度"),
    ("INV-2004", "C005",   4800,   1, "open", "轻度：逾期第 1 天，刚进入触发区"),
    ("INV-2005", "C011",  31000,   6, "open", "轻度"),
    ("INV-2006", "C002",  15200,  14, "open", "轻度边界：第 14 天，再过一天就升中度"),

    # ---- 🟡 中度：因客户历史逾期 ----
    ("INV-2007", "C003",  18000,   5, "open", "中度：客户历史逾期 1 次（金额/天数都不够格）"),
    ("INV-2008", "C006",   9900,   4, "open", "中度：客户历史逾期 2 次"),
    ("INV-2009", "C009",  42000,  11, "open", "中度：历史逾期 1 次"),
    # ---- 🟡 中度：因金额 ----
    ("INV-2010", "C001",  68000,   8, "open", "中度：金额 ≥ 5 万"),
    ("INV-2011", "C005", 125000,   3, "open", "中度：金额 12.5 万，仅逾期 3 天"),
    # ---- 🟡 中度：因逾期天数 ----
    ("INV-2012", "C007",  52300,  20, "open", "中度：天数 + 金额双命中"),
    ("INV-2013", "C008",  16700,  18, "open", "中度：逾期 18 天"),
    ("INV-2014", "C011",  27500,  25, "open", "中度：逾期 25 天"),
    ("INV-2015", "C002",  95000,  22, "open", "中度：逾期 22 天 + 金额 9.5 万"),

    # ---- 🔴 高风险：因逾期天数 ----
    ("INV-2016", "C008",   7200,  35, "open", "高风险：小额但逾期 35 天"),
    ("INV-2017", "C012",  33000,  48, "open", "高风险：逾期 48 天"),
    # ---- 🔴 高风险：因金额 ----
    ("INV-2018", "C003", 210000,   6, "open", "高风险：金额 21 万越红线，仅逾期 6 天"),
    ("INV-2019", "C006", 260000,  40, "open", "高风险：金额 + 天数双命中"),
    ("INV-2020", "C009",  88000,  62, "open", "高风险：逾期 62 天"),

    # ---- 策略层强制升级（分级引擎本判得更轻）----
    ("INV-2021", "C004",  19000,   7, "open", "P-04 VIP：规则本判轻度，VIP 强制转人工"),
    ("INV-2022", "C010", 140000,  12, "open", "P-03+P-04：VIP + 大额"),
    ("INV-2023", "C005",  21000,  10, "open", "P-01 关键词：客户提出分期 → 强制高风险"),
    ("INV-2024", "C011",  13800,  16, "open", "P-06：客户称已付款 → 暂停催收待核账"),
    ("INV-2025", "C007",  45000,  19, "open", "P-02 争议：客户提法务 → 强制高风险"),

    # ---- 未到期 / 已结清：不进入催收流 ----
    ("INV-2026", "C001",   6300,  -5, "open", "未到期：不动作"),
    ("INV-2027", "C012",  11000, -12, "open", "未到期：不动作"),
    ("INV-2028", "C003",  55000,  -2, "paid", "已结清"),
    ("INV-2029", "C008",  30000,   8, "paid", "已结清（曾逾期，现已回款）"),
    ("INV-2030", "C006",  17400,   2, "open", "中度：历史逾期 2 次，刚逾期 2 天"),
]

#: 客户回复。这些是触发 §4.2 强制升级的输入。
REPLIES = [
    ("R-001", "INV-2023", "C005", -1, "whatsapp",
     "你好，这笔款我们最近资金周转有点紧张，能不能分三期付？下个月开始每月付一部分。"),
    ("R-002", "INV-2024", "C011", -2, "whatsapp",
     "我们上周五已经转账了，回单我让财务发给你核对一下。"),
    ("R-003", "INV-2025", "C007", -1, "gmail",
     "这张发票的金额跟我们系统里对不上，差了将近一万。如果贵司坚持这个数字，我们只能让律师介入了。"),
    ("R-004", "INV-2002", "C002", -3, "whatsapp",
     "收到，本周内会安排付款，谢谢提醒。"),
    ("R-005", "INV-2013", "C008", -1, "whatsapp",
     "好的，知道了。"),
]


def seed(data_dir: Path, anchor: date | None = None) -> dict[str, int]:
    """把演示数据写入 data/。会覆盖既有 CSV，但不动 action_log。"""
    anchor = anchor or date.today()
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    terms = {c[0]: c[10] for c in CUSTOMERS}

    with open(data_dir / "customers.csv", "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(CUSTOMER_COLUMNS)
        w.writerows(CUSTOMERS)

    with open(data_dir / "receivables.csv", "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=INVOICE_COLUMNS)
        w.writeheader()
        for inv_id, cid, amount, overdue, status, intent in INVOICES:
            due = anchor - timedelta(days=overdue)
            issue = due - timedelta(days=terms[cid])
            paid = (due + timedelta(days=3)).isoformat() if status == "paid" else ""
            w.writerow({
                "invoice_id": inv_id, "customer_id": cid, "amount": amount,
                "issue_date": issue.isoformat(), "due_date": due.isoformat(),
                "status": status, "paid_date": paid,
                "agent_tier": "", "agent_stage": 0,
                "agent_state": "closed" if status == "paid" else "new",
                "agent_last_action_at": "", "agent_buffer_until": "",
                "agent_owner": "", "agent_note": intent,
            })

    with open(data_dir / "inbox.csv", "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=INBOX_COLUMNS)
        w.writeheader()
        for rid, inv_id, cid, days_ago, channel, text in REPLIES:
            received = datetime.combine(anchor + timedelta(days=days_ago),
                                        datetime.min.time()).replace(hour=10, minute=30)
            w.writerow({
                "reply_id": rid, "invoice_id": inv_id, "customer_id": cid,
                "received_at": received.isoformat(timespec="seconds"),
                "channel": channel, "text": text, "processed": "", "intent": "",
            })

    # 熔断状态与 outbox 一并清空，保证每次 seed 都是干净起点
    (data_dir / "breaker_state.json").unlink(missing_ok=True)
    outbox = data_dir / "outbox"
    if outbox.exists():
        for f in outbox.glob("*.json"):
            f.unlink()
    (data_dir / "action_log.csv").unlink(missing_ok=True)

    return {"customers": len(CUSTOMERS), "invoices": len(INVOICES), "replies": len(REPLIES)}
