"""本地可视化面板。

用标准库 http.server，不引 Flask/FastAPI —— 演示环境少一个依赖就少一个出错的可能。
后端只吐 JSON，页面在 dashboard/index.html。

面板要回答的四个问题（就是财务和 CFO 真正会问的四个）：
    1. 现在有多少钱在外面、多危险？      → KPI + 账龄
    2. 每一条为什么被这么判？             → 账款表 + 分级依据 + 命中策略
    3. Agent 到底对客户说了什么？         → outbox 原文
    4. 有没有为了效率牺牲客户关系？       → 护栏指标
"""
from __future__ import annotations

import json
import threading
import webbrowser
from datetime import date, datetime
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import report as report_mod
from .audit import AuditLog
from .config import Config
from .models import Tier
from .orchestrator import Orchestrator
from .policy import rule_catalog
from .templates import FORBIDDEN_PATTERNS, catalog as template_catalog


def build_state(root: Path, as_of: date | None = None) -> dict:
    cfg = Config.load(root / "config.json")
    orc = Orchestrator(root, cfg)
    as_of = as_of or date.today()
    now = datetime.combine(as_of, datetime.min.time()).replace(hour=9)

    customers = orc.store.load_customers()
    plan = orc.daily_scan(now=now, dry_plan=True)      # 预演，不写不发
    metrics = report_mod.compute(root, cfg, as_of)
    log = AuditLog(root / "data" / "action_log.csv").read_all()

    rows = []
    for d in plan.actions:
        inv, cust = d.invoice, d.customer
        rows.append({
            "invoice_id": inv.invoice_id,
            "customer": cust.name,
            "customer_id": cust.customer_id,
            "vip": cust.is_vip,
            "account_manager": cust.account_manager,
            "amount": inv.amount,
            "due_date": inv.due_date.isoformat() if inv.due_date else "",
            "days_overdue": d.detail.get("days_overdue", 0),
            "status": inv.status,
            "tier": d.tier.value,
            "base_tier": d.base_tier.value,
            "forced": d.tier is not d.base_tier,
            "action": d.action.value,
            "reasons": d.reasons,
            "policy_hits": d.policy_hits,
            "may_contact": d.agent_may_contact_customer,
            "state": inv.agent_state,
            "stage": inv.agent_stage,
            "owner": inv.agent_owner,
            "buffer_until": inv.agent_buffer_until,
            "prior_late": cust.prior_late_count,
        })

    outbox = []
    for f in sorted((root / "data" / "outbox").glob("*.json"))[-60:]:
        try:
            outbox.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:                                       # noqa: BLE001
            continue

    replies = []
    for r in orc._read_inbox():
        replies.append({
            "invoice_id": r.get("invoice_id", ""),
            "received_at": r.get("received_at", ""),
            "channel": r.get("channel", ""),
            "text": r.get("text", ""),
            "intent": r.get("intent", ""),
        })

    return {
        "company": cfg.company["name"],
        "as_of": as_of.isoformat(),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "live_channels": [n for n, ch in orc.channels.items() if ch.live],
        "kpi": {
            "open_amount": metrics.total_open_amount,
            "open_count": metrics.open_count,
            "overdue_amount": metrics.overdue_amount,
            "overdue_count": metrics.overdue_count,
            "dso": metrics.dso_proxy,
            "coverage": metrics.coverage_rate,
            "agent_actions": metrics.agent_actions,
            "human_actions": metrics.human_actions,
            "first_touch_hours": metrics.first_touch_hours,
            "first_touch_sample": metrics.first_touch_sample,
            "first_touch_backlog": metrics.first_touch_backlog_excluded,
        },
        "guardrails": {
            "vip_auto_contacted": metrics.vip_auto_contacted,
            "wrong_escalations": metrics.wrong_escalations,
        },
        "tiers": {
            t: {
                "count": sum(1 for r in rows if r["tier"] == t),
                "amount": sum(r["amount"] for r in rows if r["tier"] == t),
            }
            for t in ("low", "mid", "high", "none")
        },
        "aging": metrics.aging,
        "invoices": rows,
        "log": log[-200:],
        "outbox": outbox,
        "replies": replies,
        "rules": rule_catalog(),
        "templates": template_catalog(),
        "forbidden": [why for _, why in FORBIDDEN_PATTERNS],
        "am_unresponsive": metrics.am_unresponsive,
        "top_exposure": metrics.top_exposure,
    }


class Handler(BaseHTTPRequestHandler):
    root: Path = Path(".")

    def log_message(self, fmt, *args):                          # noqa: A003
        pass                                                    # 静音，别刷屏

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:                                   # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            page = (self.root / "dashboard" / "index.html").read_bytes()
            return self._send(200, page, "text/html; charset=utf-8")

        if parsed.path == "/api/state":
            qs = parse_qs(parsed.query)
            as_of = None
            if "date" in qs:
                try:
                    as_of = date.fromisoformat(qs["date"][0])
                except ValueError:
                    pass
            try:
                data = build_state(self.root, as_of)
            except Exception as exc:                            # noqa: BLE001
                body = json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)
                return self._send(500, body.encode("utf-8"), "application/json; charset=utf-8")
            body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
            return self._send(200, body, "application/json; charset=utf-8")

        self._send(404, b"not found", "text/plain; charset=utf-8")


def serve(root: Path, port: int = 8787, open_browser: bool = True) -> None:
    handler = partial(Handler)
    Handler.root = Path(root)
    url = f"http://127.0.0.1:{port}/"
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)

    print(f"\n\033[1m应收账款 Agent · 可视化面板\033[0m")
    print(f"  {url}")
    print(f"  \033[2m只监听本机回环地址，数据不出这台电脑。Ctrl+C 停止。\033[0m\n")

    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n面板已停止。")
        httpd.shutdown()
