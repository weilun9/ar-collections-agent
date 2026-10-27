"""周报层 —— PM 文档 §3.1 + §6 成功指标。

给 CFO 的那份「不用追着财务要的、随时能看的逾期健康度报告」。

报告结构刻意分三段，对应 §6 的指标设计：
    1. 现金流健康度   —— North Star 与账龄结构
    2. Agent 运行情况 —— 自动化占比、响应时效、覆盖率
    3. 护栏指标       —— 反向指标，专门回答"有没有为了效率牺牲客户关系"

第 3 段放在报告里而不是藏在监控后台，是有意的：
如果护栏指标只有做系统的人看得到，它就不会真正约束决策。
让 CFO 每周都看见"VIP 被自动催收 0 次"，这个数字才有分量。
"""
from __future__ import annotations

import html
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from .audit import AuditLog
from .config import Config
from .models import Customer, Invoice, Tier
from .store import CsvStore

AGING_BUCKETS = [(1, 14, "1-14 天"), (15, 30, "15-30 天"),
                 (31, 60, "31-60 天"), (61, 10**6, "60 天以上")]


@dataclass
class WeeklyMetrics:
    as_of: date
    total_open_amount: float = 0.0
    overdue_amount: float = 0.0
    overdue_count: int = 0
    open_count: int = 0
    dso_proxy: float = 0.0
    aging: dict[str, dict] = field(default_factory=dict)
    tier_amount: dict[str, float] = field(default_factory=dict)
    tier_count: dict[str, int] = field(default_factory=dict)
    agent_actions: int = 0
    human_actions: int = 0
    coverage_rate: float = 0.0
    first_touch_hours: float | None = None
    first_touch_sample: int = 0
    first_touch_backlog_excluded: int = 0
    am_unresponsive: list[dict] = field(default_factory=list)
    vip_auto_contacted: int = 0
    wrong_escalations: int = 0
    escalated: list[dict] = field(default_factory=list)
    top_exposure: list[dict] = field(default_factory=list)


def compute(root: Path, cfg: Config, as_of: date | None = None) -> WeeklyMetrics:
    as_of = as_of or date.today()
    store = CsvStore(root / "data")
    audit = AuditLog(root / "data" / "action_log.csv")
    customers = store.load_customers()
    invoices = store.load_invoices()
    log = audit.read_all()

    m = WeeklyMetrics(as_of=as_of)

    open_invoices = [i for i in invoices if i.is_open()]
    m.open_count = len(open_invoices)
    m.total_open_amount = sum(i.amount for i in open_invoices)

    overdue = [i for i in open_invoices if i.days_overdue(as_of) >= 1]
    m.overdue_count = len(overdue)
    m.overdue_amount = sum(i.amount for i in overdue)

    # DSO 近似：按金额加权的平均在外天数。
    # 严格 DSO 需要期间赊销收入，那个数字在 ERP 里、不在这张应收表上。
    # 这里用加权账龄作为代理指标，并在报告里明确标注 —— 宁可标注清楚，不要给一个看着精确的错数。
    if m.total_open_amount:
        weighted = sum(i.amount * max((as_of - i.issue_date).days, 0) for i in open_invoices)
        m.dso_proxy = weighted / m.total_open_amount

    for lo, hi, label in AGING_BUCKETS:
        rows = [i for i in overdue if lo <= i.days_overdue(as_of) <= hi]
        m.aging[label] = {"count": len(rows), "amount": sum(i.amount for i in rows)}

    for i in overdue:
        t = i.agent_tier or "unclassified"
        m.tier_amount[t] = m.tier_amount.get(t, 0.0) + i.amount
        m.tier_count[t] = m.tier_count.get(t, 0) + 1

    # ---- Agent vs 人工 ----
    real_actions = [r for r in log if r.get("action") not in ("hold", "skip", "batch_capped", "")]
    m.agent_actions = sum(1 for r in real_actions if r.get("actor") == "agent")
    m.human_actions = sum(1 for r in real_actions if r.get("actor") == "human")

    # ---- 覆盖率：每一条逾期账款是否都被系统处理过（目标 100%，零漏单）----
    touched = {r["invoice_id"] for r in log if r.get("invoice_id")}
    if overdue:
        m.coverage_rate = len([i for i in overdue if i.invoice_id in touched]) / len(overdue) * 100

    # ---- 首次触达时效：逾期后多久发出第一次动作 ----
    #
    # 口径说明（这一段是有意为之，面试可展开）：
    # 只统计**系统上线之后才逾期**的账款。上线那天存量里已经逾期 60 天的账款，
    # 它的"首次触达 = 1440 小时"反映的是上线前的历史欠账，不是 Agent 的响应速度。
    # 把存量算进来会得到一个又大又没意义的数字；而剔除存量之后，
    # 这个指标才真正回答"从今往后，一笔账逾期多久会被跟进"。
    inv_map = {i.invoice_id: i for i in invoices}
    go_live: datetime | None = None
    for r in log:
        ts = r.get("timestamp", "")
        if ts:
            try:
                go_live = datetime.fromisoformat(ts)
                break
            except ValueError:
                continue

    first_by_inv: dict[str, str] = {}
    for r in log:
        inv_id = r.get("invoice_id")
        if inv_id and r.get("actor") == "agent" and r.get("action") not in ("hold", "skip"):
            first_by_inv.setdefault(inv_id, r.get("timestamp", ""))

    gaps: list[float] = []
    backlog = 0
    for inv_id, ts in first_by_inv.items():
        inv = inv_map.get(inv_id)
        if not inv or not ts or not inv.due_date:
            continue
        try:
            acted = datetime.fromisoformat(ts)
        except ValueError:
            continue
        # 逾期起算点是到期日的**次日零点**：到期日当天付款仍算准时
        # （与 days_overdue >= 1 的判定保持一致）。
        # 若从到期日零点起算，每一笔都会虚增 24 小时，指标就没法跟"<24 小时"的目标比。
        overdue_since = datetime.combine(inv.due_date + timedelta(days=1), datetime.min.time())
        if go_live is not None and overdue_since < go_live.replace(
                hour=0, minute=0, second=0, microsecond=0):
            backlog += 1                      # 上线前就已逾期 → 存量，不计入
            continue
        hours = (acted - overdue_since).total_seconds() / 3600
        if hours >= 0:
            gaps.append(hours)
    m.first_touch_backlog_excluded = backlog
    if gaps:
        m.first_touch_hours = sum(gaps) / len(gaps)
        m.first_touch_sample = len(gaps)

    # ---- 客户经理未响应（§八 Q2：这本身是个管理信号）----
    unresponsive: dict[str, list] = defaultdict(list)
    for r in log:
        if "未在缓冲期内响应" in (r.get("reasons") or "") or "未响应" in (r.get("reasons") or ""):
            inv = inv_map.get(r.get("invoice_id", ""))
            if inv:
                cust = customers.get(inv.customer_id)
                if cust:
                    unresponsive[cust.account_manager].append(inv.invoice_id)
    m.am_unresponsive = [
        {"account_manager": am, "count": len(v), "invoices": v}
        for am, v in sorted(unresponsive.items(), key=lambda kv: -len(kv[1]))
    ]

    # ---- 护栏指标 ----
    vip_ids = {c.customer_id for c in customers.values() if c.is_vip}
    m.vip_auto_contacted = sum(
        1 for r in log
        if r.get("actor") == "agent"
        and r.get("customer_id") in vip_ids
        and r.get("channel") in ("whatsapp", "gmail")      # 只算对外渠道
        and r.get("ok") == "1"
    )
    # 错误升级率：本该转人工却被 Agent 自动对外处理的条数
    m.wrong_escalations = sum(
        1 for r in log
        if r.get("actor") == "agent"
        and r.get("tier") == Tier.HIGH.value
        and r.get("channel") in ("whatsapp", "gmail")
        and r.get("ok") == "1"
    )

    m.escalated = [
        {
            "invoice_id": i.invoice_id,
            "customer": customers[i.customer_id].name if i.customer_id in customers else i.customer_id,
            "amount": i.amount,
            "days": i.days_overdue(as_of),
            "note": i.agent_note,
        }
        for i in overdue if i.agent_tier == Tier.HIGH.value
    ]
    m.escalated.sort(key=lambda x: -x["amount"])

    exposure: Counter = Counter()
    for i in overdue:
        exposure[i.customer_id] += i.amount
    m.top_exposure = [
        {"customer": customers[cid].name if cid in customers else cid,
         "amount": amt,
         "vip": cid in vip_ids}
        for cid, amt in exposure.most_common(5)
    ]
    return m


def _money(v: float) -> str:
    return f"¥{v:,.0f}"


def render_markdown(m: WeeklyMetrics, cfg: Config) -> str:
    period_start = m.as_of - timedelta(days=7)
    L: list[str] = []
    add = L.append

    add(f"# 应收账款健康度周报")
    add(f"\n**{cfg.company['name']}** · 统计区间 {period_start} ~ {m.as_of}\n")
    add("> 本报告由应收账款 Agent 自动生成。所有对外催收动作的完整记录见 `data/action_log.csv`。\n")

    add("\n## 一、现金流健康度\n")
    add("| 指标 | 数值 |")
    add("|---|---|")
    add(f"| 未结清应收总额 | {_money(m.total_open_amount)}（{m.open_count} 笔） |")
    add(f"| 其中已逾期 | **{_money(m.overdue_amount)}**（{m.overdue_count} 笔） |")
    pct = (m.overdue_amount / m.total_open_amount * 100) if m.total_open_amount else 0
    add(f"| 逾期占比 | {pct:.1f}% |")
    add(f"| 加权平均在外天数（DSO 代理指标） | {m.dso_proxy:.1f} 天 |")
    add("\n> DSO 代理指标 = Σ(金额 × 在外天数) / Σ(金额)。"
        "严格 DSO 需要期间赊销收入，该数据在 ERP 侧，接入后可替换为标准口径。\n")

    add("\n### 账龄分布\n")
    add("| 账龄 | 笔数 | 金额 | 占逾期额 |")
    add("|---|---:|---:|---:|")
    for _, _, label in AGING_BUCKETS:
        b = m.aging.get(label, {"count": 0, "amount": 0})
        share = (b["amount"] / m.overdue_amount * 100) if m.overdue_amount else 0
        add(f"| {label} | {b['count']} | {_money(b['amount'])} | {share:.1f}% |")

    add("\n### 风险分级分布\n")
    add("| 分级 | 笔数 | 金额 |")
    add("|---|---:|---:|")
    for tier, label in [("low", "🟢 轻度"), ("mid", "🟡 中度"), ("high", "🔴 高风险")]:
        add(f"| {label} | {m.tier_count.get(tier, 0)} | {_money(m.tier_amount.get(tier, 0))} |")

    add("\n## 二、Agent 运行情况\n")
    total = m.agent_actions + m.human_actions
    auto_pct = (m.agent_actions / total * 100) if total else 0
    add("| 指标 | 数值 | 目标 |")
    add("|---|---|---|")
    add(f"| Agent 自主完成动作 | {m.agent_actions} 条（{auto_pct:.0f}%） | 轻度分级 80% 自主 |")
    add(f"| 人工介入动作 | {m.human_actions} 条 | — |")
    add(f"| 逾期账款覆盖率 | **{m.coverage_rate:.0f}%** | 100%（零漏单） |")
    if m.first_touch_hours is not None:
        add(f"| 逾期后首次触达 | **{m.first_touch_hours:.1f} 小时**"
            f"（{m.first_touch_sample} 笔样本） | < 24 小时（原 5-7 天） |")
    else:
        add("| 逾期后首次触达 | 样本不足 | < 24 小时（原 5-7 天） |")
    if m.first_touch_backlog_excluded:
        add("\n> 首次触达时效只统计系统上线后新逾期的账款；"
            f"上线时已存在的 {m.first_touch_backlog_excluded} 笔存量逾期不计入 —— "
            "它们的账龄反映的是上线前的历史欠账，不是 Agent 的响应速度。\n")

    if m.am_unresponsive:
        add("\n### 客户经理缓冲期响应情况\n")
        add("> 中风险账款进入 48 小时缓冲期后，客户经理未在期限内接管的条数。"
            "这本身是一个管理信号，反映响应及时性。\n")
        add("| 客户经理 | 未响应条数 | 涉及账款 |")
        add("|---|---:|---|")
        for r in m.am_unresponsive:
            add(f"| {r['account_manager']} | {r['count']} | {', '.join(r['invoices'])} |")

    add("\n## 三、护栏指标（反向指标）\n")
    add("> 这一段回答的是：为了效率，我们有没有牺牲客户关系。\n")
    add("| 指标 | 本期 | 目标 | 状态 |")
    add("|---|---:|---:|---|")
    ok = lambda v: "✅ 达标" if v == 0 else "⚠️ 需复核"       # noqa: E731
    add(f"| VIP 客户被 Agent 自动催收次数 | {m.vip_auto_contacted} | 0 | {ok(m.vip_auto_contacted)} |")
    add(f"| 错误升级（该转人工却自动对外） | {m.wrong_escalations} | 0 | {ok(m.wrong_escalations)} |")

    if m.escalated:
        add("\n## 四、需要人工决策的高风险账款\n")
        add("> 以下账款系统**未向客户发送任何自动消息**，等待人工决定下一步。\n")
        add("| 发票 | 客户 | 金额 | 逾期 | 升级原因 |")
        add("|---|---|---:|---:|---|")
        for e in m.escalated[:15]:
            add(f"| {e['invoice_id']} | {e['customer']} | {_money(e['amount'])} | "
                f"{e['days']} 天 | {e['note'][:60]} |")

    if m.top_exposure:
        add("\n## 五、逾期敞口 Top 5 客户\n")
        add("| 客户 | 逾期金额 |")
        add("|---|---:|")
        for e in m.top_exposure:
            tag = " ⭐VIP" if e["vip"] else ""
            add(f"| {e['customer']}{tag} | {_money(e['amount'])} |")

    add(f"\n---\n*生成时间：{datetime.now():%Y-%m-%d %H:%M}*")
    return "\n".join(L)


def render_html(markdown_text: str, m: WeeklyMetrics, cfg: Config) -> str:
    """极简 Markdown → HTML（只处理本报告用到的语法，不引第三方库）。"""
    out: list[str] = []
    in_table = False
    for line in markdown_text.split("\n"):
        s = line.rstrip()
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if all(set(c) <= set("-: ") and c for c in cells):
                continue
            if not in_table:
                out.append("<table>")
                in_table = True
                tag = "th"
            else:
                tag = "td"
            row = "".join(f"<{tag}>{_inline(c)}</{tag}>" for c in cells)
            out.append(f"<tr>{row}</tr>")
            continue
        if in_table:
            out.append("</table>")
            in_table = False
        if s.startswith("### "):
            out.append(f"<h3>{_inline(s[4:])}</h3>")
        elif s.startswith("## "):
            out.append(f"<h2>{_inline(s[3:])}</h2>")
        elif s.startswith("# "):
            out.append(f"<h1>{_inline(s[2:])}</h1>")
        elif s.startswith("> "):
            out.append(f"<blockquote>{_inline(s[2:])}</blockquote>")
        elif s.startswith("---"):
            out.append("<hr>")
        elif s:
            out.append(f"<p>{_inline(s)}</p>")
    if in_table:
        out.append("</table>")

    body = "\n".join(out)
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>应收账款健康度周报 · {m.as_of}</title>
<style>
 body{{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;max-width:900px;
      margin:0 auto;padding:40px 24px;color:#1a1a1a;line-height:1.7;background:#fff}}
 h1{{font-size:26px;border-bottom:3px solid #2563eb;padding-bottom:10px;margin-bottom:6px}}
 h2{{font-size:19px;margin-top:36px;color:#1e3a8a}}
 h3{{font-size:15px;margin-top:24px;color:#334155}}
 table{{border-collapse:collapse;width:100%;margin:14px 0;font-size:14px}}
 th,td{{border:1px solid #e2e8f0;padding:8px 12px;text-align:left}}
 th{{background:#f1f5f9;font-weight:600}}
 tr:nth-child(even) td{{background:#fafbfc}}
 blockquote{{border-left:3px solid #cbd5e1;margin:12px 0;padding:6px 16px;
             color:#475569;background:#f8fafc;font-size:13px}}
 hr{{border:0;border-top:1px solid #e2e8f0;margin:32px 0}}
 code{{background:#f1f5f9;padding:2px 6px;border-radius:4px;font-size:13px}}
 strong{{color:#0f172a}}
</style></head><body>
{body}
</body></html>"""


def _inline(text: str) -> str:
    t = html.escape(text)
    t = t.replace("**", "\x00")
    parts = t.split("\x00")
    if len(parts) > 1:
        t = "".join(p if n % 2 == 0 else f"<strong>{p}</strong>" for n, p in enumerate(parts))
    while "`" in t:
        a = t.find("`")
        b = t.find("`", a + 1)
        if b == -1:
            break
        t = t[:a] + "<code>" + t[a + 1:b] + "</code>" + t[b + 1:]
    return t


def generate(root: Path, cfg: Config, as_of: date | None = None) -> tuple[Path, Path, WeeklyMetrics]:
    m = compute(root, cfg, as_of)
    md = render_markdown(m, cfg)
    html_text = render_html(md, m, cfg)

    reports_dir = root / "reports"
    reports_dir.mkdir(exist_ok=True)
    stamp = (as_of or date.today()).isoformat()
    md_path = reports_dir / f"weekly-{stamp}.md"
    html_path = reports_dir / f"weekly-{stamp}.html"
    md_path.write_text(md, encoding="utf-8")
    html_path.write_text(html_text, encoding="utf-8")
    return md_path, html_path, m
