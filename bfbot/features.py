"""傾向分析（短期の値動きの手がかり）

どれも「よく使われる考え方」を自分で実装したもの。予測力があるかは
`python run.py analyze` で記録データに当てて確かめること（無いこともある）。

- trend_z : 速い EMA と遅い EMA の差を、その時間での値動きの大きさで割った値（短期トレンド）
- regime_z: 1分 EMA と 15分 EMA の差（長めの傾向。逆らう方向には入らない設定に使う）
- flow    : 直近の約定の買い/売りの偏り（テイカー側の向き、公式: side はテイカー）
- obi     : 板の上位 n 段の厚みの偏り
- ofi     : 最良気配の変化から測る注文の流れ（Cont, Kukanov, Stoikov 2014 の式）
- score   : 上を重み付きで合成した -1〜+1 の値
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


@dataclass
class FeatureConfig:
    tau_fast: float = 3.0
    tau_slow: float = 30.0
    tau_regime_fast: float = 60.0
    tau_regime_slow: float = 900.0
    tau_flow: float = 5.0
    tau_ofi: float = 3.0
    tau_vol: float = 120.0
    obi_levels: int = 5
    w_trend: float = 1.0
    w_flow: float = 1.0
    w_obi: float = 0.5
    w_ofi: float = 1.0
    warmup_sec: float = 60.0
    regime_warmup_sec: float = 300.0


class TimeEma:
    """時間で重みを付ける指数移動平均。値は次の更新まで一定とみなす（板の価格に合う扱い）。"""

    __slots__ = ("tau", "ema", "last_x", "t")

    def __init__(self, tau: float):
        self.tau = tau
        self.ema: Optional[float] = None
        self.last_x = 0.0
        self.t = 0.0

    def update(self, x: float, t: float) -> None:
        if self.ema is None:
            self.ema, self.last_x, self.t = x, x, t
            return
        dt = t - self.t
        if dt > 0:
            self.ema += (1.0 - math.exp(-dt / self.tau)) * (self.last_x - self.ema)
            self.t = t
        self.last_x = x

    def value_at(self, t: float) -> Optional[float]:
        if self.ema is None:
            return None
        dt = t - self.t
        if dt <= 0:
            return self.ema
        return self.ema + (1.0 - math.exp(-dt / self.tau)) * (self.last_x - self.ema)


class DecaySum:
    """時間とともに指数的に薄まる合計"""

    __slots__ = ("tau", "v", "t")

    def __init__(self, tau: float):
        self.tau = tau
        self.v = 0.0
        self.t: Optional[float] = None

    def _decay(self, t: float) -> None:
        if self.t is None:
            self.t = t
            return
        dt = t - self.t
        if dt > 0:
            self.v *= math.exp(-dt / self.tau)
            self.t = t

    def add(self, x: float, t: float) -> None:
        self._decay(t)
        self.v += x

    def value(self, t: float) -> float:
        self._decay(t)
        return self.v


class FeatureEngine:
    def __init__(self, cfg: FeatureConfig):
        self.cfg = cfg
        self.ema_fast = TimeEma(cfg.tau_fast)
        self.ema_slow = TimeEma(cfg.tau_slow)
        self.reg_fast = TimeEma(cfg.tau_regime_fast)
        self.reg_slow = TimeEma(cfg.tau_regime_slow)
        self.buy = DecaySum(cfg.tau_flow)
        self.sell = DecaySum(cfg.tau_flow)
        self.ofi = DecaySum(cfg.tau_ofi)
        self.depth = TimeEma(30.0)
        self.var: Optional[float] = None   # 1秒あたりの対数リターンの分散
        self._vt: Optional[float] = None
        self._vmid = 0.0
        self._prev_top = None
        self.start_t: Optional[float] = None

    def reset_flow(self) -> None:
        """切断後など、連続性が切れた時に呼ぶ"""
        self._prev_top = None
        self._vt = None

    def on_book(self, book, t: float) -> None:
        mid = book.mid
        if mid is None:
            return
        if self.start_t is None:
            self.start_t = t
        self.ema_fast.update(mid, t)
        self.ema_slow.update(mid, t)
        self.reg_fast.update(mid, t)
        self.reg_slow.update(mid, t)
        bb, ba = book.best_bid, book.best_ask
        qb, qa = book.best_bid_size(), book.best_ask_size()
        if self._prev_top is not None:
            pbb, pqb, pba, pqa = self._prev_top
            e = 0.0
            if bb >= pbb:
                e += qb
            if bb <= pbb:
                e -= pqb
            if ba <= pba:
                e -= qa
            if ba >= pba:
                e += pqa
            if e != 0.0:
                self.ofi.add(e, t)
        self._prev_top = (bb, qb, ba, qa)
        self.depth.update((qb + qa) / 2.0, t)
        if self._vt is None:
            self._vt, self._vmid = t, mid
        else:
            dt = t - self._vt
            if dt >= 1.0:
                r = math.log(mid / self._vmid)
                inst = r * r / dt
                if self.var is None:
                    self.var = inst
                else:
                    self.var += (1.0 - math.exp(-dt / self.cfg.tau_vol)) * (inst - self.var)
                self._vt, self._vmid = t, mid

    def on_execution(self, side: str, size: float, price: float, t: float) -> None:
        if side == "BUY":
            self.buy.add(size, t)
        elif side == "SELL":
            self.sell.add(size, t)

    def compute(self, book, t: float) -> dict:
        c = self.cfg
        mid = book.mid
        bb, ba = book.best_bid, book.best_ask
        f = {"t": t, "mid": mid, "bid": bb, "ask": ba, "warm": False, "regime_ready": False,
             "score": 0.0, "trend_z": 0.0, "regime_z": 0.0, "flow": 0.0, "obi": 0.0, "ofi": 0.0,
             "spread_bps": 0.0, "vol_bps_1m": None}
        if mid is None or mid <= 0:
            return f
        f["spread_bps"] = (ba - bb) / mid * 1e4
        sigma = math.sqrt(self.var) if self.var else 0.0
        if sigma > 0:
            f["vol_bps_1m"] = sigma * math.sqrt(60.0) * 1e4
        ef, es = self.ema_fast.value_at(t), self.ema_slow.value_at(t)
        rf, rs = self.reg_fast.value_at(t), self.reg_slow.value_at(t)
        if sigma > 0 and ef and es:
            f["trend_z"] = math.log(ef / es) / (sigma * math.sqrt(c.tau_slow))
        if sigma > 0 and rf and rs:
            f["regime_z"] = math.log(rf / rs) / (sigma * math.sqrt(c.tau_regime_slow))
        b, s = self.buy.value(t), self.sell.value(t)
        f["flow"] = (b - s) / (b + s) if b + s > 1e-12 else 0.0
        f["obi"] = book.imbalance(c.obi_levels)
        depth = self.depth.value_at(t) or 0.0
        f["ofi"] = self.ofi.value(t) / depth if depth > 1e-12 else 0.0
        wsum = abs(c.w_trend) + abs(c.w_flow) + abs(c.w_obi) + abs(c.w_ofi)
        if wsum <= 0:
            wsum = 1.0
        f["score"] = (c.w_trend * math.tanh(f["trend_z"] / 2.0) + c.w_flow * f["flow"]
                      + c.w_obi * f["obi"] + c.w_ofi * math.tanh(f["ofi"])) / wsum
        age = t - (self.start_t if self.start_t is not None else t)
        f["warm"] = age >= c.warmup_sec and sigma > 0
        f["regime_ready"] = age >= c.regime_warmup_sec
        return f
