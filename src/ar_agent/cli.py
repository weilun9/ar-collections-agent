"""命令行入口。

常用命令：
    python -m ar_agent.cli matrix          看分级矩阵（不产生任何动作）
    python -m ar_agent.cli scan            跑一次每日扫描
    python -m ar_agent.cli dashboard       打开可视化面板
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path

# Windows 控制台默认 GBK，中文与 emoji 会直接抛 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:                                       # noqa: BLE001
        pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ar_agent import report as report_mod                   # noqa: E402
from ar_agent import templates as tpl                       # noqa: E402
from ar_agent.audit import AuditLog                         # noqa: E402
from ar_agent.config import Config                          # noqa: E402
from ar_agent.models import ActionType, Message, Tier       # noqa: E402
from ar_agent.orchestrator import INBOX_COLUMNS, Orchestrator  # noqa: E402
from ar_agent.policy import rule_catalog                    # noqa: E402
from ar_agent.seed import seed as seed_data                 # noqa: E402

ROOT = Path(__file__).resolve().parents[2]

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
GREEN, YELLOW, RED, BLUE, GREY = ("\033[32m", "\033[33m", "\033[31m", "\033[36m", "\033[90m")

TIER_STYLE = {
    "low": (GREEN, "🟢 轻度"),
    "mid": (YELLOW, "🟡 中度"),
    "high": (RED, "🔴 高风险"),
    "none": (GREY, "—  未触发"),
}


def _enable_ansi() -> None:
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            k.SetConsoleMode(k.GetStdHandle(-11), 7)
        except Exception:                                   # noqa: BLE001
            pass


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise SystemExit(f"无法解析日期: {s}（支持 YYYY-MM-DD 或 'YYYY-MM-DD HH:MM'）")


def _orc() -> Orchestrator:
    return Orchestrator(ROOT)


def _rule(char: str = "─", n: int = 88) -> str:
    return GREY + char * n + RESET


def _w(text: str) -> int:
    """终端显示宽度：中日韩字符占 2 格，其余占 1 格。

    直接用 len() 排版会让含中文的表格全部错位。
    """
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _pad(text: str, width: int, align: str = "<") -> str:
    """按显示宽度补齐（不能用 f-string 的 :<n，它按字符数算）。"""
    gap = max(width - _w(text), 0)
    if align == ">":
        return " " * gap + text
    return text + " " * gap


# --------------------------------------------------------------------------- #
# 命令
# --------------------------------------------------------------------------- #

def cmd_seed(args) -> None:
    anchor = (_parse_dt(args.today) or datetime.now()).date()
    stats = seed_data(ROOT / "data", anchor)
    print(f"{GREEN}✓{RESET} 演示数据已生成（锚点日 {anchor}）")
    print(f"  客户 {stats['customers']} 家 · 账款 {stats['invoices']} 笔 · 客户回复 {stats['replies']} 条")
    print(f"  {DIM}data/customers.csv · data/receivables.csv · data/inbox.csv{RESET}")
    print(f"\n  下一步：{BOLD}python -m ar_agent.cli matrix{RESET}")


def cmd_matrix(args) -> None:
    """只读预演：显示每条账款会被怎么判、为什么，但不执行任何动作。"""
    orc = _orc()
    now = _parse_dt(args.date) or datetime.now()
    rep = orc.daily_scan(now=now, dry_plan=True)

    print(f"\n{BOLD}分级决策矩阵{RESET}  {DIM}（预演模式，不发送任何消息、不写回状态）{RESET}")
    print(f"{DIM}扫描时点：{now:%Y-%m-%d %H:%M}{RESET}\n")
    print(_rule())
    print(BOLD + _pad("发票", 11) + _pad("客户", 12) + _pad("金额", 12, ">")
          + "  " + _pad("逾期", 7, ">") + "  " + _pad("分级", 10) + "动作" + RESET)
    print(_rule())

    order = {"high": 0, "mid": 1, "low": 2, "none": 3}
    for d in sorted(rep.actions, key=lambda x: (order[x.tier.value], -x.invoice.amount)):
        color, label = TIER_STYLE[d.tier.value]
        days = d.detail.get("days_overdue", 0)
        days_s = f"{days}天" if days >= 1 else "未逾期"
        if not d.invoice.is_open():
            days_s = "已结清"
        moved = "" if d.tier is d.base_tier else f"  {DIM}(原判 {TIER_STYLE[d.base_tier.value][1]}){RESET}"
        print(_pad(d.invoice.invoice_id, 11)
              + _pad(d.customer.name, 12)
              + _pad("¥" + format(d.invoice.amount, ",.0f"), 12, ">")
              + "  " + _pad(days_s, 7, ">")
              + "  " + color + _pad(label, 10) + RESET
              + _pad(_action_label(d.action), 20) + moved)
        if args.verbose or d.policy_hits:
            for r in d.reasons:
                print(f"    {GREY}· {r}{RESET}")
            for h in d.policy_hits:
                print(f"    {RED}⚑ {h}{RESET}")
    print(_rule())

    counts = rep.by_tier
    print(f"\n共 {rep.scanned} 笔　"
          f"{GREEN}🟢 轻度 {counts.get('low', 0)}{RESET}　"
          f"{YELLOW}🟡 中度 {counts.get('mid', 0)}{RESET}　"
          f"{RED}🔴 高风险 {counts.get('high', 0)}{RESET}　"
          f"{GREY}未触发 {counts.get('none', 0)}{RESET}")

    forced = [d for d in rep.actions if d.tier is not d.base_tier]
    if forced:
        print(f"\n{BOLD}被 L4 策略层强制升级的账款（{len(forced)} 笔）{RESET}"
              f"　{DIM}—— 分级引擎本来判得更轻，策略层覆盖了它{RESET}")
        for d in forced:
            print(f"  {d.invoice.invoice_id}  {d.customer.name}  "
                  f"{DIM}{d.base_tier.value} → {RESET}{RED}{d.tier.value}{RESET}")
            for h in d.policy_hits:
                print(f"      {RED}⚑{RESET} {h}")


def _action_label(a: ActionType) -> str:
    return {
        ActionType.WHATSAPP_REMINDER: "WhatsApp 提醒",
        ActionType.GMAIL_DUNNING: "邮件催收函",
        ActionType.SLACK_AM_NOTICE: "Slack 通知客户经理",
        ActionType.SLACK_FINANCE_ALERT: "Slack 告警财务",
        ActionType.GMAIL_FINANCE_ALERT: "邮件告警财务",
        ActionType.ESCALATE: "升级人工（不外发）",
        ActionType.HOLD: "暂不动作",
        ActionType.SKIP: "跳过",
    }.get(a, a.value)


def cmd_scan(args) -> None:
    orc = _orc()
    now = _parse_dt(args.date) or datetime.now()
    live = any(orc.channels[c].live for c in orc.channels)

    print(f"\n{BOLD}每日扫描{RESET}  {DIM}{now:%Y-%m-%d %H:%M}{RESET}")
    if live:
        print(f"{RED}⚠ 实发模式：本次会真的向客户/同事发送消息{RESET}")
    else:
        print(f"{DIM}dry-run 模式：消息写入 data/outbox/，不会真的发出{RESET}")
    print()

    rep = orc.daily_scan(now=now)

    acted = [d for d in rep.actions if d.action not in (ActionType.SKIP, ActionType.HOLD)]
    for d in acted:
        color, label = TIER_STYLE[d.tier.value]
        print(f"  {color}●{RESET} " + _pad(d.invoice.invoice_id, 11)
              + _pad(d.customer.name, 12)
              + color + _pad(label, 10) + RESET
              + "→  " + _action_label(d.action))
        for h in d.policy_hits:
            print(f"      {RED}⚑{RESET} {h}")

    held = [d for d in rep.actions if d.action is ActionType.HOLD]
    if held:
        print(f"\n  {DIM}暂不动作 {len(held)} 笔：{RESET}")
        for d in held[:8]:
            # 策略层的原因比分级原因更能解释"为什么按住不动"，优先显示
            reason = d.policy_hits[-1] if d.policy_hits else (d.reasons[-1] if d.reasons else "")
            mark = f"{RED}⚑{RESET} " if d.policy_hits else ""
            print(f"    {GREY}○ " + _pad(d.invoice.invoice_id, 11) + _pad(d.customer.name, 12)
                  + RESET + mark + f"{GREY}{reason}{RESET}")
        if len(held) > 8:
            print(f"    {GREY}… 另有 {len(held) - 8} 笔{RESET}")

    print(f"\n{_rule()}")
    ok = sum(1 for _, _, s in rep.sends if s)
    print(f"扫描 {rep.scanned} 笔　执行动作 {len(acted)} 项　消息发出 {ok}/{len(rep.sends)} 条"
          f"　升级人工 {len(rep.escalations)} 笔")
    if rep.inbound_processed:
        print(f"处理客户回复 {rep.inbound_processed} 条")
    if rep.deferred:
        print(f"{YELLOW}熔断：{rep.deferred} 笔顺延至下一批{RESET}")
    if rep.halted:
        print(f"{RED}熔断：{rep.halt_reason}{RESET}")
    print(f"{DIM}run_id={rep.run_id} · 完整记录见 data/action_log.csv{RESET}")
    if not live and rep.sends:
        print(f"{DIM}消息内容见 data/outbox/（python -m ar_agent.cli outbox 可查看）{RESET}")


def cmd_takeover(args) -> None:
    _orc().takeover(args.invoice_id, args.by, args.note or "")
    print(f"{GREEN}✓{RESET} {args.invoice_id} 已由 {BOLD}{args.by}{RESET} 接管，Agent 停止对它的一切自动动作")


def cmd_release(args) -> None:
    _orc().release(args.invoice_id, args.by)
    print(f"{GREEN}✓{RESET} {args.invoice_id} 已交回 Agent 继续跟进")


def cmd_paid(args) -> None:
    on = (_parse_dt(args.on) or datetime.now()).date()
    _orc().mark_paid(args.invoice_id, on)
    print(f"{GREEN}✓{RESET} {args.invoice_id} 已标记回款（{on}）")


def cmd_reply(args) -> None:
    """模拟一条客户回复，用来现场演示关键词强制升级。"""
    from ar_agent.inbound import classify_reply

    orc = _orc()
    rows = orc._read_inbox()
    inv = next((i for i in orc.store.load_invoices() if i.invoice_id == args.invoice_id), None)
    if inv is None:
        raise SystemExit(f"账款不存在: {args.invoice_id}")
    when = _parse_dt(args.at) or datetime.now()
    rows.append({
        "reply_id": f"R-{len(rows) + 1:03d}", "invoice_id": args.invoice_id,
        "customer_id": inv.customer_id, "received_at": when.isoformat(timespec="seconds"),
        "channel": args.channel, "text": args.text, "processed": "", "intent": "",
    })
    orc._write_inbox(rows)

    a = classify_reply(args.text)
    print(f"{GREEN}✓{RESET} 已记录 {args.invoice_id} 的客户回复")
    print(f"  {DIM}原文：{args.text}{RESET}")
    print(f"  意图判定：{BOLD}{a.intent.label}{RESET}"
          + (f"　命中关键词：{'、'.join(a.matched)}" if a.matched else ""))
    if a.forces_escalation:
        print(f"  {RED}⚑ 命中强制升级类别 —— 下次扫描时该账款将无条件转为高风险、停止自动化{RESET}")


def cmd_log(args) -> None:
    log = AuditLog(ROOT / "data" / "action_log.csv").read_all()
    if args.invoice:
        log = [r for r in log if r.get("invoice_id") == args.invoice]
    if args.actor:
        log = [r for r in log if r.get("actor") == args.actor]
    if not args.all:
        log = [r for r in log if r.get("action") not in ("hold", "skip")]
    log = log[-args.limit:]

    if not log:
        print(f"{DIM}无匹配记录{RESET}")
        return

    print(f"\n{BOLD}审计日志{RESET}  {DIM}L5 可观测性：每条记录都能回答「谁做的、对谁、为什么」{RESET}\n")
    print(_rule())
    print(BOLD + _pad("时间", 18) + _pad("谁", 9) + _pad("发票", 11)
          + _pad("动作", 24) + _pad("渠道", 16) + "模式" + RESET)
    print(_rule())
    for r in log:
        actor = r.get("actor", "")
        tag = {"agent": BLUE + _pad("Agent", 9) + RESET,
               "human": YELLOW + _pad("人工", 9) + RESET,
               "system": GREY + _pad("系统", 9) + RESET}.get(actor, _pad(actor, 9))
        print(_pad(r.get("timestamp", "")[:16], 18) + tag
              + _pad(r.get("invoice_id", "") or "-", 11)
              + _pad(r.get("action", ""), 24)
              + _pad(r.get("channel", "") or "-", 16)
              + (r.get("mode", "") or "-"))
        if args.verbose:
            if r.get("reasons"):
                print(f"    {GREY}· {r['reasons']}{RESET}")
            if r.get("policy_hits"):
                print(f"    {RED}⚑ {r['policy_hits']}{RESET}")
    print(_rule())
    agent_n = sum(1 for r in log if r.get("actor") == "agent")
    human_n = sum(1 for r in log if r.get("actor") == "human")
    print(f"共 {len(log)} 条　{BLUE}Agent {agent_n}{RESET}　{YELLOW}人工 {human_n}{RESET}"
          f"　{GREY}系统 {len(log) - agent_n - human_n}{RESET}")


def cmd_outbox(args) -> None:
    files = sorted((ROOT / "data" / "outbox").glob("*.json"))
    if args.channel:
        files = [f for f in files if f"_{args.channel}_" in f.name]
    files = files[-args.limit:]
    if not files:
        print(f"{DIM}outbox 为空 —— 先跑一次 scan{RESET}")
        return
    for f in files:
        d = json.loads(f.read_text(encoding="utf-8"))
        aud = "对客户" if d["audience"] == "customer" else "对内部"
        color = RED if d["audience"] == "customer" else BLUE
        print(f"\n{_rule('═')}")
        print(f"{color}[{d['channel']} · {aud}]{RESET} → {BOLD}{d['to']}{RESET}"
              f"　{DIM}模板 {d['template_id']} · {d['invoice_id']} · {d['mode']}{RESET}")
        if d["subject"]:
            print(f"{BOLD}主题：{d['subject']}{RESET}")
        print(_rule("─"))
        print(d["body"])
    print(f"\n{_rule('═')}")
    print(f"{DIM}共 {len(files)} 条（全部为 dry-run，未真实发送）{RESET}")


def cmd_report(args) -> None:
    orc = _orc()
    as_of = (_parse_dt(args.date) or datetime.now()).date()
    md_path, html_path, m = report_mod.generate(ROOT, orc.cfg, as_of)

    print(f"\n{GREEN}✓{RESET} 周报已生成")
    print(f"  {md_path.relative_to(ROOT)}")
    print(f"  {html_path.relative_to(ROOT)}  {DIM}← 双击用浏览器打开{RESET}\n")
    print(_rule())
    print(f"未结清应收 {BOLD}¥{m.total_open_amount:,.0f}{RESET}"
          f"　其中逾期 {RED}¥{m.overdue_amount:,.0f}{RESET}（{m.overdue_count} 笔）")
    print(f"DSO 代理指标 {m.dso_proxy:.1f} 天　逾期覆盖率 {m.coverage_rate:.0f}%")
    print(f"Agent 动作 {m.agent_actions} 条　人工动作 {m.human_actions} 条")
    print(f"护栏：VIP 被自动催收 {m.vip_auto_contacted} 次（目标 0）　"
          f"错误升级 {m.wrong_escalations} 次（目标 0）")
    print(_rule())

    if args.send:
        subject = f"[周报] 应收账款健康度 · {as_of}"
        body = md_path.read_text(encoding="utf-8")
        msg = Message("gmail_internal", orc.cfg.company["cfo_email"], subject, body,
                      "WEEKLY_REPORT", "", "", "internal")
        res = orc.channels["gmail_internal"].send(msg)
        orc.audit.write(run_id="report", actor="agent", action="weekly_report",
                        channel="gmail_internal", recipient=orc.cfg.company["cfo_email"],
                        mode=res.mode, ok="1" if res.ok else "0",
                        reasons=[f"周报 {as_of}"])
        print(f"\n{GREEN}✓{RESET} 已投递给 CFO（{res.mode}）：{orc.cfg.company['cfo_email']}")


def cmd_rules(args) -> None:
    print(f"\n{BOLD}L4 运行时策略层{RESET}  {DIM}—— 跑在 LLM 之外的硬规则，命中即执行，Agent 无法绕过{RESET}\n")
    for r in rule_catalog():
        print(f"  {RED}{r['id']}{RESET}  {r['description']}")

    print(f"\n{BOLD}L2 预设模板{RESET}  {DIM}—— Agent 只能选模板 + 填结构化变量，无自由文本入口{RESET}\n")
    print("  " + DIM + _pad("模板 ID", 24) + _pad("渠道", 18) + "受众" + RESET)
    for t in tpl.catalog():
        color = RED if t["audience"] == "customer" else BLUE
        aud = "对客户" if t["audience"] == "customer" else "对内部"
        print("  " + _pad(t["id"], 24) + _pad(t["channel"], 18) + color + aud + RESET)
        print(f"    {GREY}{t['note']}{RESET}")

    print(f"\n{BOLD}发送前内容闸门{RESET}  {DIM}—— 对外模板渲染后再扫一遍禁用措辞{RESET}\n")
    for _, why in tpl.FORBIDDEN_PATTERNS:
        print(f"  {GREY}·{RESET} {why}")

    orc = _orc()
    print(f"\n{BOLD}L3 Agent 可写字段白名单{RESET}  {DIM}—— 金额/账期/联系方式不在内，越权写入直接抛异常{RESET}\n")
    print(f"  {GREEN}可写{RESET}：{'、'.join(sorted(orc.store.AGENT_WRITABLE_FIELDS))}")
    print(f"  {RED}只读{RESET}：invoice_id、customer_id、amount、issue_date、due_date、status、paid_date")


def cmd_reset_breaker(args) -> None:
    orc = _orc()
    orc.breaker.reset(args.channel)
    target = args.channel or "全部渠道"
    print(f"{GREEN}✓{RESET} 熔断已复位：{target}")


def cmd_verify(args) -> None:
    """自检：把风险边界逐条跑一遍，输出通过/失败。"""
    from ar_agent.verify import run_checks
    run_checks(ROOT)


def cmd_demo(args) -> None:
    from ar_agent.demo import run_demo
    run_demo(ROOT, pause=not args.no_pause)


def cmd_dashboard(args) -> None:
    from ar_agent.dashboard import serve
    serve(ROOT, args.port, open_browser=not args.no_open)


# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ar", description="应收账款催收 Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""常用流程:
  python -m ar_agent.cli seed              生成演示数据
  python -m ar_agent.cli matrix -v         看分级决策（不动作）
  python -m ar_agent.cli scan              跑一次每日扫描
  python -m ar_agent.cli outbox            看 Agent 发了什么
  python -m ar_agent.cli report            生成 CFO 周报
  python -m ar_agent.cli dashboard         打开可视化面板
  python -m ar_agent.cli demo              一键跑完整演示剧本
""")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("seed", help="生成演示数据")
    s.add_argument("--today", help="锚点日 YYYY-MM-DD（默认今天）")
    s.set_defaults(func=cmd_seed)

    s = sub.add_parser("matrix", help="预演分级结果，不执行任何动作")
    s.add_argument("--date", help="模拟扫描时点")
    s.add_argument("-v", "--verbose", action="store_true", help="显示每条的分级依据")
    s.set_defaults(func=cmd_matrix)

    s = sub.add_parser("scan", help="执行每日扫描")
    s.add_argument("--date", help="模拟扫描时点，可用于推演未来几天")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("takeover", help="人工接管一条账款，Agent 立即停止")
    s.add_argument("invoice_id")
    s.add_argument("--by", required=True, help="接管人")
    s.add_argument("--note", help="备注")
    s.set_defaults(func=cmd_takeover)

    s = sub.add_parser("release", help="交回给 Agent 继续跟进")
    s.add_argument("invoice_id")
    s.add_argument("--by", required=True)
    s.set_defaults(func=cmd_release)

    s = sub.add_parser("paid", help="标记回款（人工动作）")
    s.add_argument("invoice_id")
    s.add_argument("--on", help="回款日期")
    s.set_defaults(func=cmd_paid)

    s = sub.add_parser("reply", help="模拟一条客户回复")
    s.add_argument("invoice_id")
    s.add_argument("--text", required=True)
    s.add_argument("--channel", default="whatsapp")
    s.add_argument("--at", help="回复时间")
    s.set_defaults(func=cmd_reply)

    s = sub.add_parser("log", help="查审计日志")
    s.add_argument("--invoice")
    s.add_argument("--actor", choices=["agent", "human", "system"])
    s.add_argument("--limit", type=int, default=40)
    s.add_argument("--all", action="store_true", help="含 hold/skip 记录")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(func=cmd_log)

    s = sub.add_parser("outbox", help="查看 Agent 发出的消息全文")
    s.add_argument("--limit", type=int, default=5)
    s.add_argument("--channel", choices=["whatsapp", "gmail", "gmail_internal", "slack"])
    s.set_defaults(func=cmd_outbox)

    s = sub.add_parser("report", help="生成 CFO 周报")
    s.add_argument("--date")
    s.add_argument("--send", action="store_true", help="同时投递给 CFO")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("rules", help="展示风险边界设计（L2/L3/L4）")
    s.set_defaults(func=cmd_rules)

    s = sub.add_parser("verify", help="自检：逐条验证风险边界是否真的生效")
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("reset-breaker", help="复位熔断")
    s.add_argument("--channel")
    s.set_defaults(func=cmd_reset_breaker)

    s = sub.add_parser("demo", help="一键跑完整演示剧本")
    s.add_argument("--no-pause", action="store_true", help="不等待回车，连续跑完")
    s.set_defaults(func=cmd_demo)

    s = sub.add_parser("dashboard", help="启动可视化面板")
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--no-open", action="store_true")
    s.set_defaults(func=cmd_dashboard)

    return p


def main(argv: list[str] | None = None) -> int:
    _enable_ansi()
    args = build_parser().parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
