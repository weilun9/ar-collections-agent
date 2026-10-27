"""L6 熔断机制 —— PM 文档 §5。

两条熔断：
  1. 单次扫描最多处理 N 条账款，超过则分批（防一次任务失控地跑完全表）；
  2. 连续 N 次发送失败自动暂停并告警（防渠道挂了还在空转、或凭据失效被风控）。

熔断状态持久化在 data/breaker_state.json —— 必须跨进程存活，
否则"连续失败 5 次"这个计数在每次扫描重启时归零，等于没有熔断。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

from .config import Config


@dataclass
class BreakerState:
    consecutive_failures: dict[str, int] = field(default_factory=dict)
    tripped: dict[str, str] = field(default_factory=dict)     # channel -> 触发时间
    sends_by_date: dict[str, int] = field(default_factory=dict)


class CircuitBreaker:
    def __init__(self, path: Path, cfg: Config):
        self.path = Path(path)
        self.cfg = cfg
        self.state = self._load()

    def _load(self) -> BreakerState:
        if self.path.exists():
            with open(self.path, encoding="utf-8") as fh:
                return BreakerState(**json.load(fh))
        return BreakerState()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(asdict(self.state), fh, ensure_ascii=False, indent=2)

    # ---- 熔断 1：扫描批量上限 ----
    def batch(self, invoices: list) -> tuple[list, list]:
        """返回 (本批处理, 顺延到下一批)。"""
        cap = self.cfg.breaker["max_invoices_per_scan"]
        return invoices[:cap], invoices[cap:]

    # ---- 熔断 2：连续发送失败 ----
    def is_tripped(self, channel: str) -> bool:
        return channel in self.state.tripped

    def record_send(self, channel: str, ok: bool, on_date: date) -> bool:
        """记录一次发送结果。返回 True 表示本次调用触发了熔断。"""
        if ok:
            self.state.consecutive_failures[channel] = 0
            key = on_date.isoformat()
            self.state.sends_by_date[key] = self.state.sends_by_date.get(key, 0) + 1
            return False

        n = self.state.consecutive_failures.get(channel, 0) + 1
        self.state.consecutive_failures[channel] = n
        threshold = self.cfg.breaker["consecutive_send_failures_to_pause"]
        if n >= threshold and channel not in self.state.tripped:
            self.state.tripped[channel] = datetime.now().isoformat(timespec="seconds")
            return True
        return False

    def sends_on(self, on_date: date) -> int:
        return self.state.sends_by_date.get(on_date.isoformat(), 0)

    def reset(self, channel: str | None = None) -> None:
        """人工确认渠道恢复后手动复位。刻意不做自动恢复 —— 熔断了就该有人去看一眼。"""
        if channel:
            self.state.tripped.pop(channel, None)
            self.state.consecutive_failures[channel] = 0
        else:
            self.state.tripped.clear()
            self.state.consecutive_failures.clear()
        self.save()
