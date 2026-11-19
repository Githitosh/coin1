"""手元の板（Realtime API のスナップショット＋差分から組み立てる）

公式:
- lightning_board_snapshot_{product}: 板全体（配信頻度は抑えられている、配列の並び順は保証なし）
- lightning_board_{product}: 変化した価格だけ。size 0 はその価格の注文がなくなったこと。
  板寄せの成行は price 0 で届く（板には載せない）
"""

from __future__ import annotations

from bisect import bisect_left, insort
from typing import Dict, Iterator, List, Optional, Tuple


class OrderBook:
    def __init__(self) -> None:
        self.bids: Dict[float, float] = {}
        self.asks: Dict[float, float] = {}
        self._bp: List[float] = []  # 昇順
        self._ap: List[float] = []  # 昇順
        self.ready = False
        self.last_update = 0.0
        self.updates = 0
        self.uncross_fixes = 0

    def reset(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self._bp.clear()
        self._ap.clear()
        self.ready = False

    @staticmethod
    def _levels(levels) -> Iterator[Tuple[float, float]]:
        for lv in levels or ():
            try:
                yield float(lv["price"]), float(lv["size"])
            except (KeyError, TypeError, ValueError):
                continue

    def apply_snapshot(self, msg: dict, ts: float) -> None:
        msg = msg or {}
        self.bids = {p: s for p, s in self._levels(msg.get("bids")) if p > 0 and s > 0}
        self.asks = {p: s for p, s in self._levels(msg.get("asks")) if p > 0 and s > 0}
        self._bp = sorted(self.bids)
        self._ap = sorted(self.asks)
        self.ready = bool(self._bp and self._ap)
        self.last_update = ts
        self.updates += 1
        self._uncross(set(), set())

    def apply_diff(self, msg: dict, ts: float) -> None:
        msg = msg or {}
        nb, na = set(), set()
        for p, s in self._levels(msg.get("bids")):
            if p <= 0:
                continue
            self._set(self.bids, self._bp, p, s)
            if s > 0:
                nb.add(p)
        for p, s in self._levels(msg.get("asks")):
            if p <= 0:
                continue
            self._set(self.asks, self._ap, p, s)
            if s > 0:
                na.add(p)
        self.last_update = ts
        self.updates += 1
        if self.ready:
            self._uncross(nb, na)
            if not (self._bp and self._ap):
                self.ready = False

    @staticmethod
    def _set(d: Dict[float, float], prices: List[float], p: float, s: float) -> None:
        if s <= 0:
            if d.pop(p, None) is not None:
                i = bisect_left(prices, p)
                if i < len(prices) and prices[i] == p:
                    del prices[i]
        else:
            if p not in d:
                insort(prices, p)
            d[p] = s

    def _uncross(self, new_bids: set, new_asks: set) -> None:
        """買いの最良が売りの最良以上になった（ありえない）時、古い側の気配を外す。"""
        guard = 0
        while self._bp and self._ap and self._bp[-1] >= self._ap[0] and guard < 100000:
            guard += 1
            self.uncross_fixes += 1
            bb, ba = self._bp[-1], self._ap[0]
            if bb in new_bids and ba not in new_asks:
                self._set(self.asks, self._ap, ba, 0)
            elif ba in new_asks and bb not in new_bids:
                self._set(self.bids, self._bp, bb, 0)
            else:
                # どちらが新しいか分からない時は両方外す（次のスナップショットで正しく戻る）
                self._set(self.asks, self._ap, ba, 0)
                self._set(self.bids, self._bp, bb, 0)

    # ------------------------------------------------------------- 参照
    @property
    def best_bid(self) -> Optional[float]:
        return self._bp[-1] if self._bp else None

    @property
    def best_ask(self) -> Optional[float]:
        return self._ap[0] if self._ap else None

    def best_bid_size(self) -> float:
        return self.bids[self._bp[-1]] if self._bp else 0.0

    def best_ask_size(self) -> float:
        return self.asks[self._ap[0]] if self._ap else 0.0

    @property
    def mid(self) -> Optional[float]:
        if not (self._bp and self._ap):
            return None
        return (self._bp[-1] + self._ap[0]) / 2.0

    @property
    def spread(self) -> Optional[float]:
        if not (self._bp and self._ap):
            return None
        return self._ap[0] - self._bp[-1]

    def microprice(self) -> Optional[float]:
        if not (self._bp and self._ap):
            return None
        bb, ba = self._bp[-1], self._ap[0]
        qb, qa = self.bids[bb], self.asks[ba]
        return (bb * qa + ba * qb) / (qa + qb)

    def bid_levels(self) -> Iterator[Tuple[float, float]]:
        """買い板を高い順に"""
        for p in reversed(self._bp):
            yield p, self.bids[p]

    def ask_levels(self) -> Iterator[Tuple[float, float]]:
        """売り板を安い順に"""
        for p in self._ap:
            yield p, self.asks[p]

    def imbalance(self, n: int = 5) -> float:
        """上位 n 段の厚みの偏り（+1 = 買いだけ、-1 = 売りだけ）"""
        sb = sum(self.bids[p] for p in self._bp[-n:]) if self._bp else 0.0
        sa = sum(self.asks[p] for p in self._ap[:n]) if self._ap else 0.0
        tot = sb + sa
        return (sb - sa) / tot if tot > 0 else 0.0
