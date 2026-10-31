"""Generate demo data.

The data is **designed branch by branch**, not random: the 30 invoices deliberately cover every cell of the decision matrix
and every rule in the L4 policy layer, so every branch has data to verify it.

All dates are relative to an anchor day, so re-seeding at any time gives the same ageing profile
and the demo never drifts because the data has sat around for two weeks.
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
    # id     name        contact  WhatsApp        email domain          AM    slack        VIP late-history avg-days-to-pay terms
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

#: (invoice number, customer, amount, days overdue [negative = not yet due], status, design intent)
INVOICES = [
    # ---- 🟢 Light: handled by the agent ----
    ("INV-2001", "C001",  12000,   3, "open", "light: first WhatsApp reminder"),
    ("INV-2002", "C002",   8500,   9, "open", "light: customer replied with an ack; no escalation"),
    ("INV-2003", "C007",  23400,  12, "open", "light"),
    ("INV-2004", "C005",   4800,   1, "open", "light: day 1 overdue, just entered the trigger zone"),
    ("INV-2005", "C011",  31000,   6, "open", "light"),
    ("INV-2006", "C002",  15200,  14, "open", "light boundary: day 14, one more day and it becomes medium"),

    # ---- 🟡 Medium: because of the customer's late-payment history ----
    ("INV-2007", "C003",  18000,   5, "open", "medium: customer paid late once before (amount and days alone would not qualify)"),
    ("INV-2008", "C006",   9900,   4, "open", "medium: customer paid late twice before"),
    ("INV-2009", "C009",  42000,  11, "open", "medium: paid late once before"),
    # ---- 🟡 Medium: because of the amount ----
    ("INV-2010", "C001",  68000,   8, "open", "medium: amount ≥ 50,000"),
    ("INV-2011", "C005", 125000,   3, "open", "medium: amount 125,000, only 3 days overdue"),
    # ---- 🟡 Medium: because of days overdue ----
    ("INV-2012", "C007",  52300,  20, "open", "medium: both days and amount match"),
    ("INV-2013", "C008",  16700,  18, "open", "medium: 18 days overdue"),
    ("INV-2014", "C011",  27500,  25, "open", "medium: 25 days overdue"),
    ("INV-2015", "C002",  95000,  22, "open", "medium: 22 days overdue + amount 95,000"),

    # ---- 🔴 High risk: because of days overdue ----
    ("INV-2016", "C008",   7200,  35, "open", "high risk: small amount but 35 days overdue"),
    ("INV-2017", "C012",  33000,  48, "open", "high risk: 48 days overdue"),
    # ---- 🔴 High risk: because of the amount ----
    ("INV-2018", "C003", 210000,   6, "open", "high risk: amount 210,000 is over the ceiling, only 6 days overdue"),
    ("INV-2019", "C006", 260000,  40, "open", "high risk: both amount and days match"),
    ("INV-2020", "C009",  88000,  62, "open", "high risk: 62 days overdue"),

    # ---- Forced up by the policy layer (the tiering engine alone would rate them lower) ----
    ("INV-2021", "C004",  19000,   7, "open", "P-04 VIP: rules say light, VIP forces it to a person"),
    ("INV-2022", "C010", 140000,  12, "open", "P-03+P-04: VIP + large amount"),
    ("INV-2023", "C005",  21000,  10, "open", "P-01 keyword: customer asks for instalments -> forced high risk"),
    ("INV-2024", "C011",  13800,  16, "open", "P-06: customer says it has paid -> collection paused until finance checks"),
    ("INV-2025", "C007",  45000,  19, "open", "P-02 dispute: customer mentions lawyers -> forced high risk"),

    # ---- Not yet due / settled: not in the collections flow ----
    ("INV-2026", "C001",   6300,  -5, "open", "not yet due: no action"),
    ("INV-2027", "C012",  11000, -12, "open", "not yet due: no action"),
    ("INV-2028", "C003",  55000,  -2, "paid", "settled"),
    ("INV-2029", "C008",  30000,   8, "paid", "settled (was overdue, now paid)"),
    ("INV-2030", "C006",  17400,   2, "open", "medium: paid late twice before, 2 days overdue"),
]

#: Customer replies (customer-facing data, kept in Chinese). These are the inputs that trigger the §4.2 forced escalation.
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
    """Write the demo data into data/. Overwrites the existing CSVs and clears the action log, outbox and breaker state."""
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

    # Clear the breaker state and outbox too, so every seed starts clean
    (data_dir / "breaker_state.json").unlink(missing_ok=True)
    outbox = data_dir / "outbox"
    if outbox.exists():
        for f in outbox.glob("*.json"):
            f.unlink()
    (data_dir / "action_log.csv").unlink(missing_ok=True)

    return {"customers": len(CUSTOMERS), "invoices": len(INVOICES), "replies": len(REPLIES)}
