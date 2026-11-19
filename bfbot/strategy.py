"""売買の判断（板・約定が届くたびに呼ばれる）

- 同時に出す注文は1本だけ（二重発注・決済しすぎを防ぐ）
- 入る: score が閾値を超え、スプレッドとボラティリティが範囲内、長めの傾向に逆らわない時
- 出る: 損切り / 利確 / 保有時間切れ / 信号の反転 / 停止
- 出口はいつもテイカー（IOC）で確実に。入口はテイカーかメイカーを選べる
- 見せ玉のような「約定させる気のない注文」は出さない設計（出す注文はすべて約定が目的）
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Tuple

from .broker import EPS, Order


@dataclass
class StrategyConfig:
    size: float = 0.001
    min_size: float = 0.001
    order_mode: str = "taker"          # taker / maker
    entry_threshold: float = 0.45
    exit_on_reverse: float = 0.25
    take_profit_bps: float = 8.0
    stop_loss_bps: float = 6.0
    max_hold_sec: float = 45.0
    cooldown_sec: float = 3.0
    taker_slippage_bps: float = 3.0
    exit_slippage_bps: float = 8.0
    maker_timeout_sec: float = 4.0
    maker_improve_ticks: int = 0
    max_spread_bps: float = 4.0
    min_vol_bps_1m: float = 0.5
    max_vol_bps_1m: float = 150.0
    allow_short: bool = True
    trend_filter: bool = True
    tick: float = 1.0
    minute_to_expire: int = 1


class Strategy:
    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg
        self.last_entry_t = -1e18
        self.signals = 0
        self.exit_reasons: dict = {}

    def _up(self, x: float) -> float:
        return math.ceil(x / self.cfg.tick - 1e-9) * self.cfg.tick

    def _down(self, x: float) -> float:
        return math.floor(x / self.cfg.tick + 1e-9) * self.cfg.tick

    def _taker(self, side, size, book, purpose, reason, slip_bps) -> Order:
        if side == "BUY":
            px = self._up(book.best_ask * (1 + slip_bps / 1e4))
        else:
            px = self._down(book.best_bid * (1 - slip_bps / 1e4))
        return Order(side=side, size=round(size, 8), price=px, tif="IOC", purpose=purpose,
                     reason=reason, minute_to_expire=self.cfg.minute_to_expire)

    def _maker(self, side, size, book, purpose, reason) -> Order:
        imp = self.cfg.maker_improve_ticks * self.cfg.tick
        if side == "BUY":
            px = book.best_bid + imp
            if px >= book.best_ask:
                px = book.best_bid
        else:
            px = book.best_ask - imp
            if px <= book.best_bid:
                px = book.best_ask
        return Order(side=side, size=round(size, 8), price=px, tif="GTC", purpose=purpose,
                     reason=reason, minute_to_expire=self.cfg.minute_to_expire)

    def decide(self, f: dict, book, broker, risk, t: float) -> List[Tuple[str, object]]:
        c = self.cfg
        working = broker.working_orders()
        if working:
            acts: List[Tuple[str, object]] = []
            for o in working:
                if o.cancel_requested or o.status != "ACTIVE" or o.tif != "GTC":
                    continue
                s = f["score"]
                fading = ((o.side == "BUY" and s < c.entry_threshold * 0.5)
                          or (o.side == "SELL" and s > -c.entry_threshold * 0.5))
                if t - o.created_t >= c.maker_timeout_sec or (o.purpose == "entry" and fading):
                    acts.append(("cancel", o))
            return acts

        pos = broker.position
        apos = abs(pos)
        if EPS < apos < c.min_size - EPS:
            side = "BUY" if pos > 0 else "SELL"
            return [("order", self._taker(side, c.min_size, book, "dust", "端数整理", c.exit_slippage_bps))]

        if apos >= c.min_size - EPS:
            long = pos > 0
            px = book.best_bid if long else book.best_ask
            entry = broker.avg_price
            pnl_bps = ((px - entry) / entry * 1e4) * (1 if long else -1) if entry > 0 else 0.0
            open_t = broker.position_open_t
            held = t - open_t if open_t is not None else 0.0
            s = f["score"]
            reason = None
            if risk.halted:
                reason = "停止"
            elif pnl_bps <= -c.stop_loss_bps:
                reason = "損切り"
            elif pnl_bps >= c.take_profit_bps:
                reason = "利確"
            elif held >= c.max_hold_sec:
                reason = "時間切れ"
            elif (long and s <= -c.exit_on_reverse) or ((not long) and s >= c.exit_on_reverse):
                reason = "信号反転"
            if reason is None:
                return []
            self.exit_reasons[reason] = self.exit_reasons.get(reason, 0) + 1
            side = "SELL" if long else "BUY"
            return [("order", self._taker(side, apos, book, "exit", reason, c.exit_slippage_bps))]

        if not f.get("warm"):
            return []
        ok, _ = risk.can_open(t, t - book.last_update)
        if not ok or t - self.last_entry_t < c.cooldown_sec:
            return []
        if f["spread_bps"] > c.max_spread_bps:
            return []
        vol = f.get("vol_bps_1m")
        if vol is None or vol < c.min_vol_bps_1m or vol > c.max_vol_bps_1m:
            return []
        s = f["score"]
        side = None
        if s >= c.entry_threshold:
            side = "BUY"
        elif s <= -c.entry_threshold and c.allow_short:
            side = "SELL"
        if side is None:
            return []
        if c.trend_filter and f.get("regime_ready"):
            rz = f.get("regime_z", 0.0)
            if (side == "BUY" and rz < 0) or (side == "SELL" and rz > 0):
                return []
        self.signals += 1
        reason = f"score={s:+.2f}"
        if c.order_mode == "maker":
            o = self._maker(side, c.size, book, "entry", reason)
        else:
            o = self._taker(side, c.size, book, "entry", reason, c.taker_slippage_bps)
        return [("order", o)]

    def on_submitted(self, order: Order, t: float) -> None:
        if order.purpose == "entry":
            self.last_entry_t = t
