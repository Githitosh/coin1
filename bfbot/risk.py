"""安全装置（損失の上限・注文の頻度・データ遅延・取引所の状態）"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import List, Tuple

from .util import jst_date

log = logging.getLogger("bfbot")


@dataclass
class RiskConfig:
    max_position_btc: float = 0.002
    max_daily_loss_jpy: float = 3000.0
    max_drawdown_jpy: float = 5000.0
    max_consecutive_losses: int = 5
    pause_after_losses_sec: float = 600.0
    max_orders_per_min: int = 20          # 公式の注文系上限（5分300回）よりかなり低く
    min_order_interval_sec: float = 0.5
    stale_data_sec: float = 3.0
    max_api_errors_per_5min: int = 10
    allowed_health: List[str] = field(default_factory=lambda: ["NORMAL", "BUSY", "VERY BUSY"])


class RiskManager:
    def __init__(self, cfg: RiskConfig, start_equity: float, require_exchange_status: bool = False):
        self.cfg = cfg
        self.start_equity = start_equity
        self.peak = start_equity
        self.day = None
        self.day_start_equity = start_equity
        self.halted = False
        self.halt_reason = ""
        self.consec_losses = 0
        self.pause_until = 0.0
        self.order_times: deque = deque()
        self.last_order_t = -1e18
        self.api_errors: deque = deque()
        self.exchange_ok = not require_exchange_status
        self.exchange_status = "未確認" if require_exchange_status else "-"

    def halt(self, reason: str, t: float) -> None:
        if not self.halted:
            self.halted = True
            self.halt_reason = reason
            log.error(f"【停止】{reason}。新規の売買を止め、建玉を決済します。")

    def update_equity(self, equity: float, t: float) -> None:
        d = jst_date(t)
        if self.day != d:
            self.day = d
            self.day_start_equity = equity
        if equity > self.peak:
            self.peak = equity
        if self.halted:
            return
        if self.day_start_equity - equity >= self.cfg.max_daily_loss_jpy:
            self.halt(f"1日の損失が上限 {self.cfg.max_daily_loss_jpy:,.0f} 円に達した", t)
        elif self.peak - equity >= self.cfg.max_drawdown_jpy:
            self.halt(f"最高値からの下落が上限 {self.cfg.max_drawdown_jpy:,.0f} 円に達した", t)

    def on_round_trip(self, pnl: float, t: float) -> None:
        if pnl < 0:
            self.consec_losses += 1
            if self.consec_losses >= self.cfg.max_consecutive_losses:
                self.pause_until = t + self.cfg.pause_after_losses_sec
                log.warning(f"{self.consec_losses}連敗のため {self.cfg.pause_after_losses_sec:.0f} 秒休みます")
                self.consec_losses = 0
        else:
            self.consec_losses = 0

    def order_allowed(self, t: float, is_exit: bool = False) -> Tuple[bool, str]:
        if t - self.last_order_t < self.cfg.min_order_interval_sec:
            return False, "注文間隔"
        while self.order_times and t - self.order_times[0] >= 60.0:
            self.order_times.popleft()
        cap = self.cfg.max_orders_per_min * (2 if is_exit else 1)  # 決済には余裕を持たせる
        if len(self.order_times) >= cap:
            return False, "注文回数"
        return True, ""

    def note_order(self, t: float) -> None:
        self.order_times.append(t)
        self.last_order_t = t

    def can_open(self, t: float, book_age: float) -> Tuple[bool, str]:
        if self.halted:
            return False, "停止中"
        if t < self.pause_until:
            return False, "連敗で休止中"
        if book_age > self.cfg.stale_data_sec:
            return False, "データ遅延"
        if not self.exchange_ok:
            return False, f"取引所の状態 {self.exchange_status}"
        return True, ""

    def note_api_error(self, t: float) -> None:
        self.api_errors.append(t)
        while self.api_errors and t - self.api_errors[0] > 300:
            self.api_errors.popleft()
        if len(self.api_errors) >= self.cfg.max_api_errors_per_5min:
            self.halt(f"APIエラーが5分で{len(self.api_errors)}回", t)

    def set_exchange_status(self, health: str, state: str) -> None:
        self.exchange_status = f"{health}/{state}"
        self.exchange_ok = health in self.cfg.allowed_health and state == "RUNNING"
