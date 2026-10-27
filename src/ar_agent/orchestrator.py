"""每日扫描主流程 —— PM 文档 §3.1。

一条账款在一次扫描里的完整旅程：

    读取 → [熔断:批量上限] → 分级引擎(§4) → 收件箱意图(§4.2)
         → L4 策略链 → 动作决策 → [熔断:发送失败] → 渠道发送 → 写回状态 → 审计日志

关于"Agent"这个词：这套流程的**决策部分完全是确定性代码**，没有一次 LLM 调用。
LLM 在设计里的位置是可选增强（给高风险告警生成沟通历史摘要，见 summarize_history / summarizer.py），
即使它完全不可用，催收流程照常跑、分级照常准。这是刻意的架构选择 ——
把"不能出错的部分"和"出错也只是体验变差的部分"分开。
"""
from __future__ import annotations

import csv
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from . import policy as policy_mod
from . import templates as tpl
from .audit import AuditLog
from .breaker import CircuitBreaker
from .channels import build_registry
from .config import Config
from .inbound import ReplyIntent, classify_reply
from .models import (
    ActionType,
    Actor,
    AgentState,
    Customer,
    Decision,
    Invoice,
    Message,
    Tier,
)
from .store import CsvStore
from .summarizer import Summarizer

INBOX_COLUMNS = ["reply_id", "invoice_id", "customer_id", "received_at",
                 "channel", "text", "processed", "intent"]


@dataclass
class ScanReport:
    run_id: str
    run_at: datetime
    scanned: int = 0
    deferred: int = 0                 # 因批量上限顺延到下一批
    by_tier: dict[str, int] = field(default_factory=dict)
    actions: list[Decision] = field(default_factory=list)
    sends: list[tuple[str, str, bool]] = field(default_factory=list)
    halted: bool = False
    halt_reason: str = ""
    inbound_processed: int = 0
    escalations: list[str] = field(default_factory=list)

    def count(self, tier: Tier) -> None:
        self.by_tier[tier.value] = self.by_tier.get(tier.value, 0) + 1


class Orchestrator:
    def __init__(self, root: Path, cfg: Config | None = None):
        self.root = Path(root)
        self.cfg = cfg or Config.load(self.root / "config.json")
        self.data_dir = self.root / "data"
        self.store = CsvStore(self.data_dir)
        self.audit = AuditLog(self.data_dir / "action_log.csv")
        self.breaker = CircuitBreaker(self.data_dir / "breaker_state.json", self.cfg)
        self.channels = build_registry(self.cfg, self.data_dir / "outbox")
        self.inbox_path = self.data_dir / "inbox.csv"
        self.summarizer = Summarizer(self.cfg)

    # ------------------------------------------------------------------ #
    # 收件箱
    # ------------------------------------------------------------------ #
    def _read_inbox(self) -> list[dict[str, str]]:
        if not self.inbox_path.exists():
            return []
        with open(self.inbox_path, encoding="utf-8-sig", newline="") as fh:
            return list(csv.DictReader(fh))

    def _write_inbox(self, rows: list[dict[str, str]]) -> None:
        with open(self.inbox_path, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=INBOX_COLUMNS)
            w.writeheader()
            for r in rows:
                w.writerow({c: r.get(c, "") for c in INBOX_COLUMNS})

    def _pending_intents(self, upto: datetime) -> dict[str, tuple[str, str]]:
        """扫描客户回复，返回 {invoice_id: (intent, summary)}。

        只看截止到 upto 的回复，这样 --date 回放历史时不会"看到未来的消息"。
        同一账款有多条回复时，取风险最高的那条（TERMS_CHANGE > DISPUTE > ...）。
        """
        result: dict[str, tuple[str, str]] = {}
        best_rank: dict[str, int] = {}
        order = {i.value: n for n, i in enumerate(reversed(
            [ReplyIntent.UNKNOWN, ReplyIntent.ACK, ReplyIntent.PAID_CLAIM,
             ReplyIntent.DISPUTE, ReplyIntent.TERMS_CHANGE]))}

        for row in self._read_inbox():
            received = (row.get("received_at") or "").strip()
            if received:
                try:
                    if datetime.fromisoformat(received) > upto:
                        continue
                except ValueError:
                    pass
            analysis = classify_reply(row.get("text", ""))
            if analysis.intent is ReplyIntent.UNKNOWN:
                continue
            inv = row["invoice_id"].strip()
            rank = order.get(analysis.intent.value, 0)
            if rank >= best_rank.get(inv, -1):
                best_rank[inv] = rank
                result[inv] = (analysis.intent.value, analysis.summary())
        return result

    def process_inbox(self, upto: datetime | None = None) -> int:
        """把收件箱里未处理的回复打上意图标签并落盘（供 Dashboard / 审计查看）。"""
        upto = upto or datetime.now()
        rows = self._read_inbox()
        n = 0
        for row in rows:
            if (row.get("processed") or "").strip() in ("1", "true", "yes"):
                continue
            received = (row.get("received_at") or "").strip()
            if received:
                try:
                    if datetime.fromisoformat(received) > upto:
                        continue
                except ValueError:
                    pass
            analysis = classify_reply(row.get("text", ""))
            row["intent"] = analysis.intent.value
            row["processed"] = "1"
            n += 1
        if n:
            self._write_inbox(rows)
        return n

    # ------------------------------------------------------------------ #
    # 决策
    # ------------------------------------------------------------------ #
    def decide(
        self,
        inv: Invoice,
        cust: Customer,
        now: datetime,
        intents: dict[str, tuple[str, str]],
        sends_today: int,
    ) -> Decision:
        from .tiering import classify

        today = now.date()
        tier_res = classify(inv, cust, self.cfg, today)

        intent, intent_summary = intents.get(inv.invoice_id, (None, ""))
        ctx = policy_mod.PolicyContext(
            invoice=inv, customer=cust, base_tier=tier_res.tier, today=today,
            sends_today=sends_today, inbound_intent=intent, inbound_summary=intent_summary,
        )
        outcome = policy_mod.evaluate(ctx, self.cfg)

        d = Decision(
            invoice=inv, customer=cust, tier=outcome.tier, base_tier=tier_res.tier,
            action=ActionType.SKIP, reasons=list(tier_res.reasons),
            policy_hits=list(outcome.hits),
            agent_may_contact_customer=outcome.agent_may_contact_customer,
            detail={"days_overdue": tier_res.days_overdue, "halt": outcome.halt_all_sending},
        )

        # ---- 终态：已结清 / 已被人接管 / 已升级，Agent 一律不动 ----
        if not inv.is_open():
            d.action = ActionType.SKIP
            d.add_reason("账款已结清")
            return d

        state = AgentState(inv.agent_state) if inv.agent_state else AgentState.NEW
        if state in (AgentState.HUMAN_TAKEN, AgentState.ESCALATED):
            d.action = ActionType.HOLD
            d.add_reason(
                "已由人工接管，Agent 停止一切动作"
                if state is AgentState.HUMAN_TAKEN
                else "已升级人工处理，Agent 不再对外发送"
            )
            return d

        if d.tier is Tier.NONE:
            d.action = ActionType.SKIP
            return d

        # ---- 🔴 高风险：只通知人，绝不对外 ----
        if d.tier is Tier.HIGH:
            d.action = ActionType.ESCALATE
            return d

        # ---- 策略层禁止对外接触（VIP / 已付款声明 / 日发送量熔断等）----
        if not d.agent_may_contact_customer:
            d.action = ActionType.ESCALATE if outcome.tier is Tier.HIGH else ActionType.HOLD
            return d

        # ---- 🟡 中度：48 小时缓冲期状态机 ----
        if d.tier is Tier.MID:
            buffer_hours = self.cfg.cadence["mid_am_buffer_hours"]
            if state is not AgentState.AWAITING_AM or not inv.agent_buffer_until:
                d.action = ActionType.SLACK_AM_NOTICE
                d.detail["buffer_until"] = (now + timedelta(hours=buffer_hours)).isoformat(timespec="seconds")
                d.detail["buffer_hours"] = buffer_hours
                d.add_reason(f"通知客户经理 {cust.account_manager}，给予 {buffer_hours} 小时优先处理权")
                return d

            deadline = datetime.fromisoformat(inv.agent_buffer_until)
            if now < deadline:
                d.action = ActionType.HOLD
                remaining = deadline - now
                hrs = remaining.total_seconds() / 3600
                d.add_reason(f"客户经理缓冲期内，剩余 {hrs:.1f} 小时，Agent 暂不动作")
                return d

            d.action = ActionType.GMAIL_DUNNING
            d.add_reason(f"客户经理 {cust.account_manager} 在 {buffer_hours} 小时内未响应，Agent 补位发送正式对账函")
            d.detail["am_unresponsive"] = True
            return d

        # ---- 🟢 轻度：按节奏自动跟进 ----
        cad = self.cfg.cadence
        if inv.agent_stage >= cad["low_max_reminders"]:
            d.action = ActionType.HOLD
            d.add_reason(
                f"已发出 {inv.agent_stage} 次提醒，达到轻度分级上限 "
                f"{cad['low_max_reminders']} 次；继续逾期将自然升级为中度"
            )
            return d

        if inv.agent_stage == 0:
            d.action = ActionType.WHATSAPP_REMINDER
            d.detail["template_id"] = "WA_LOW_1"
            d.add_reason("首次自动提醒")
            return d

        gap = cad["low_second_reminder_gap_days"]
        last = inv.agent_last_action_at
        if last:
            try:
                since = (now - datetime.fromisoformat(last)).days
            except ValueError:
                since = gap
            if since < gap:
                d.action = ActionType.HOLD
                d.add_reason(f"距上次提醒 {since} 天，未满 {gap} 天间隔")
                return d

        d.action = ActionType.WHATSAPP_REMINDER
        d.detail["template_id"] = "WA_LOW_2"
        d.add_reason(f"距上次提醒已满 {gap} 天，发送第 2 次提醒")
        return d

    # ------------------------------------------------------------------ #
    # 执行
    # ------------------------------------------------------------------ #
    def _vars(self, d: Decision, now: datetime) -> dict:
        inv, cust = d.invoice, d.customer
        return {
            "company": self.cfg.company["name"],
            "contact_name": cust.contact_name,
            "customer_name": cust.name,
            "account_manager": cust.account_manager,
            "invoice_id": inv.invoice_id,
            "amount": f"{inv.amount:,.2f}",
            "issue_date": inv.issue_date.isoformat() if inv.issue_date else "",
            "due_date": inv.due_date.isoformat() if inv.due_date else "",
            "days_overdue": d.detail.get("days_overdue", 0),
            "payment_url": self.cfg.company["payment_portal_url"] + inv.invoice_id,
            "reasons": "；".join(d.reasons) or "—",
            "policy_hits": "；".join(d.policy_hits) or "无",
            "history": self.summarize_history(inv.invoice_id),
            "buffer_hours": d.detail.get("buffer_hours", self.cfg.cadence["mid_am_buffer_hours"]),
            "buffer_until": d.detail.get("buffer_until", ""),
            "timestamp": now.isoformat(timespec="seconds"),
            "reason": d.detail.get("halt_reason", ""),
            "channel": d.detail.get("channel", ""),
        }

    def history_facts(self, invoice_id: str) -> list[str]:
        """确定性的沟通事实清单(最近 5 条动作 + 3 条客户回复)，按时间排序。"""
        rows = [r for r in self.audit.read_all() if r.get("invoice_id") == invoice_id]
        replies = [r for r in self._read_inbox() if r.get("invoice_id") == invoice_id]
        parts = []
        for r in rows[-5:]:
            if r.get("action") in ("hold", "skip"):
                continue
            who = "Agent" if r.get("actor") == "agent" else "人工"
            parts.append(f"{r.get('timestamp', '')[:16]} {who} 通过 {r.get('channel') or '-'} {r.get('action')}")
        for r in replies[-3:]:
            txt = (r.get("text") or "").replace("\n", " ")[:60]
            parts.append(f"{(r.get('received_at') or '')[:16]} 客户回复：「{txt}」")
        return sorted(parts)

    def summarize_history(self, invoice_id: str) -> str:
        """沟通历史摘要，附在高风险告警里给财务看 —— 整个 Agent 唯一调用 LLM 的位置。

        配置了 llm.provider 时由模型压缩成一段话，并保留原始记录供核对；
        未配置、调用失败或摘要未通过校验时，回退为确定性清单。详见 summarizer.py。
        """
        result = self.summarizer.summarize(self.history_facts(invoice_id))
        if result.note:
            return f"{result.text}\n  （{result.note}）"
        return result.text

    _ACTION_PLAN: dict[ActionType, tuple[str, str]] = {
        ActionType.WHATSAPP_REMINDER: ("whatsapp", ""),          # 模板由 decide() 指定
        ActionType.GMAIL_DUNNING: ("gmail", "MAIL_MID_DUNNING"),
        ActionType.SLACK_AM_NOTICE: ("slack", "SLACK_AM_BUFFER"),
    }

    def execute(self, d: Decision, report: ScanReport, now: datetime) -> None:
        inv, cust = d.invoice, d.customer
        updates: dict[str, object] = {"agent_tier": d.tier.value}

        def log(action: str, channel: str = "", recipient: str = "",
                template_id: str = "", mode: str = "", ok: str = "",
                preview: str = "", actor: str = Actor.AGENT.value) -> None:
            self.audit.write(
                timestamp=now.isoformat(timespec="seconds"), run_id=report.run_id, actor=actor,
                invoice_id=inv.invoice_id, customer_id=cust.customer_id, customer_name=cust.name,
                tier=d.tier.value, base_tier=d.base_tier.value, action=action, channel=channel,
                recipient=recipient, template_id=template_id, mode=mode, ok=ok,
                reasons=d.reasons, policy_hits=d.policy_hits, content_preview=preview,
            )

        def deliver(channel_key: str, template_id: str, to: str) -> bool:
            channel = self.channels[channel_key]
            if self.breaker.is_tripped(channel.name):
                log(d.action.value, channel.name, to, template_id, "blocked", "0",
                    "渠道已熔断，跳过")
                return False
            subject, body = tpl.render(template_id, self._vars(d, now))
            msg = Message(
                channel=channel.name, to=to, subject=subject, body=body,
                template_id=template_id, invoice_id=inv.invoice_id,
                customer_id=cust.customer_id, audience=channel.audience,
            )
            res = channel.send(msg)
            tripped = self.breaker.record_send(channel.name, res.ok, now.date())
            report.sends.append((channel.name, to, res.ok))
            log(d.action.value, channel.name, to, template_id, res.mode,
                "1" if res.ok else "0", body[:160].replace("\n", " "))
            if tripped:
                self._alert_breaker(channel.name, report, now,
                                    f"连续 {self.cfg.breaker['consecutive_send_failures_to_pause']} 次发送失败")
            return res.ok

        # ---------------- 各动作分支 ----------------
        if d.action in (ActionType.SKIP, ActionType.HOLD):
            if not inv.is_open():
                updates["agent_state"] = AgentState.CLOSED.value
            log(d.action.value, preview="；".join(d.reasons)[:160])

        elif d.action is ActionType.ESCALATE:
            # 高风险：Slack + 邮件双通道通知内部，**不对客户发送任何内容**
            deliver("slack", "SLACK_HIGH_ALERT", cust.am_slack_id or "#ar-alerts")
            d_action_backup = d.action
            d.action = ActionType.GMAIL_FINANCE_ALERT
            deliver("gmail_internal", "MAIL_FINANCE_ALERT", self.cfg.company["finance_lead_email"])
            d.action = d_action_backup
            updates["agent_state"] = AgentState.ESCALATED.value
            updates["agent_owner"] = "finance_lead"
            updates["agent_note"] = "；".join(d.policy_hits or d.reasons)[:200]
            updates["agent_last_action_at"] = now.isoformat(timespec="seconds")
            report.escalations.append(inv.invoice_id)

        elif d.action is ActionType.SLACK_AM_NOTICE:
            ok = deliver("slack", "SLACK_AM_BUFFER", cust.am_slack_id or "#ar-collections")
            if ok:
                updates["agent_state"] = AgentState.AWAITING_AM.value
                updates["agent_buffer_until"] = d.detail["buffer_until"]
                updates["agent_owner"] = cust.account_manager
                updates["agent_last_action_at"] = now.isoformat(timespec="seconds")

        elif d.action is ActionType.WHATSAPP_REMINDER:
            template_id = d.detail["template_id"]
            if deliver("whatsapp", template_id, cust.whatsapp):
                updates["agent_stage"] = inv.agent_stage + 1
                updates["agent_state"] = AgentState.AUTO.value
                updates["agent_last_action_at"] = now.isoformat(timespec="seconds")

        elif d.action is ActionType.GMAIL_DUNNING:
            if deliver("gmail", "MAIL_MID_DUNNING", cust.email):
                updates["agent_stage"] = inv.agent_stage + 1
                updates["agent_state"] = AgentState.AUTO.value
                updates["agent_last_action_at"] = now.isoformat(timespec="seconds")
                updates["agent_note"] = "客户经理未在缓冲期内响应，Agent 补位"

        self.store.update_agent_fields(inv.invoice_id, **updates)

    def _alert_breaker(self, channel: str, report: ScanReport, now: datetime, reason: str) -> None:
        subject, body = tpl.render("SLACK_BREAKER", {
            "reason": reason, "timestamp": now.isoformat(timespec="seconds"), "channel": channel,
        })
        msg = Message("slack", "#ar-alerts", subject, body, "SLACK_BREAKER", "", "", "internal")
        self.channels["slack"].send(msg)
        self.audit.write(
            timestamp=now.isoformat(timespec="seconds"), run_id=report.run_id,
            actor=Actor.SYSTEM.value, action="breaker_tripped", channel=channel,
            template_id="SLACK_BREAKER", reasons=[reason],
        )

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #
    def daily_scan(self, now: datetime | None = None, dry_plan: bool = False) -> ScanReport:
        now = now or datetime.now()
        report = ScanReport(run_id=uuid.uuid4().hex[:8], run_at=now)

        # 预演模式必须完全无副作用：matrix 命令和 Dashboard 都走这条路径，
        # 一个"只是看看"的操作不该把收件箱标记成已处理。
        if not dry_plan:
            report.inbound_processed = self.process_inbox(upto=now)
        intents = self._pending_intents(upto=now)

        customers = self.store.load_customers()
        invoices = [i for i in self.store.load_invoices()]

        # L6 熔断 1：单次扫描批量上限
        batch, deferred = self.breaker.batch(invoices)
        report.deferred = len(deferred)
        if deferred and not dry_plan:
            self.audit.write(
                timestamp=now.isoformat(timespec="seconds"), run_id=report.run_id,
                actor=Actor.SYSTEM.value, action="batch_capped",
                reasons=[f"本次扫描 {len(batch)} 条，{len(deferred)} 条顺延至下一批"
                         f"（上限 {self.cfg.breaker['max_invoices_per_scan']}）"],
            )

        sends_today = self.breaker.sends_on(now.date())

        for inv in batch:
            cust = customers.get(inv.customer_id)
            if cust is None:
                self.audit.write(
                    timestamp=now.isoformat(timespec="seconds"), run_id=report.run_id,
                    actor=Actor.SYSTEM.value, invoice_id=inv.invoice_id, action="skip",
                    reasons=[f"客户主数据缺失: {inv.customer_id}"],
                )
                continue

            report.scanned += 1
            d = self.decide(inv, cust, now, intents, sends_today)
            report.count(d.tier)
            report.actions.append(d)

            if d.detail.get("halt") and not report.halted:
                report.halted = True
                report.halt_reason = f"单日对外发送量已达上限 {self.cfg.policy['daily_send_cap']} 条"
                self._alert_breaker("all", report, now, report.halt_reason)

            if dry_plan:
                continue

            self.execute(d, report, now)
            sends_today = self.breaker.sends_on(now.date())

        self.breaker.save()
        return report

    # ------------------------------------------------------------------ #
    # 人工接管
    # ------------------------------------------------------------------ #
    def takeover(self, invoice_id: str, by: str, note: str = "") -> None:
        """客户经理/财务显式接管一条账款，Agent 立即停止对它的一切动作。

        刻意做成显式命令而不是"在 Slack 回一句话就算接管"：
        接管是一次授权变更，需要一个明确的、可审计的动作。
        """
        now = datetime.now()
        self.store.update_agent_fields(
            invoice_id,
            agent_state=AgentState.HUMAN_TAKEN.value,
            agent_owner=by,
            agent_last_action_at=now.isoformat(timespec="seconds"),
            agent_note=note or f"{by} 于 {now:%Y-%m-%d %H:%M} 接管",
        )
        self.audit.write(
            timestamp=now.isoformat(timespec="seconds"), run_id="manual",
            actor=Actor.HUMAN.value, invoice_id=invoice_id, action="takeover",
            reasons=[f"{by} 接管，Agent 停止自动跟进"], content_preview=note,
        )

    def release(self, invoice_id: str, by: str) -> None:
        """人工放回给 Agent 继续跟进。"""
        now = datetime.now()
        self.store.update_agent_fields(
            invoice_id,
            agent_state=AgentState.NEW.value, agent_owner="", agent_buffer_until="",
            agent_note=f"{by} 于 {now:%Y-%m-%d %H:%M} 交回 Agent",
        )
        self.audit.write(
            timestamp=now.isoformat(timespec="seconds"), run_id="manual",
            actor=Actor.HUMAN.value, invoice_id=invoice_id, action="release",
            reasons=[f"{by} 交回 Agent 继续跟进"],
        )

    def mark_paid(self, invoice_id: str, on: date | None = None) -> None:
        """标记已回款。

        注意：这个方法**不属于 Agent 的能力** —— 它写的是 status/paid_date 这类业务字段，
        而 Agent 的 Service Account 对这些字段没有写权限（见 store/base.py 白名单）。
        这里走的是财务人员身份的直接改表，CLI 里也标注为人工动作。
        """
        rows = self.store._read_raw()
        on = on or date.today()
        for row in rows:
            if row["invoice_id"].strip() == invoice_id:
                row["status"] = "paid"
                row["paid_date"] = on.isoformat()
                row["agent_state"] = AgentState.CLOSED.value
                break
        else:
            raise KeyError(f"账款不存在: {invoice_id}")
        self.store._write_raw(rows)
        self.audit.write(
            run_id="manual", actor=Actor.HUMAN.value, invoice_id=invoice_id,
            action="mark_paid", reasons=[f"财务确认回款 {on.isoformat()}"],
        )
