"""注文の執行（PaperBroker = 模擬、LiveBroker = 本番）

本番の注意（公式の仕様から）:
- Crypto CFD は反対売買で決済。建玉より多く反対注文すると差分は新規建てになる
  → 決済注文は必ず建玉と同じ数量にし、同時に1本しか出さない
- 最小発注数量 0.001 BTC-CFD。端数が残ったら同方向に足してから決済する
- 発注の通信が途中で切れたら受付の有無が分からない → 全取消してから建玉を照合
"""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from .rest import APIError, BitflyerREST, LocalRateLimit, UncertainOrderError

log = logging.getLogger("bfbot")
EPS = 1e-9
DONE = ("FILLED", "CANCELED", "EXPIRED", "REJECTED")


@dataclass
class Order:
    side: str
    size: float
    price: float
    tif: str = "IOC"            # IOC = 即時約定しない分は取消（テイカー） / GTC = 板に置く（メイカー）
    purpose: str = "entry"      # entry / exit / dust
    reason: str = ""
    minute_to_expire: int = 1
    local_id: int = 0
    created_t: float = 0.0
    sent_t: float = 0.0
    active_t: float = 0.0
    acceptance_id: Optional[str] = None
    child_order_id: Optional[str] = None
    status: str = "NEW"
    filled: float = 0.0
    fill_value: float = 0.0
    cancel_requested: bool = False
    cancel_t: Optional[float] = None
    reject_reason: str = ""
    ack_ms: Optional[float] = None
    checking: bool = False

    @property
    def remaining(self) -> float:
        return max(round(self.size - self.filled, 8), 0.0)

    @property
    def avg_fill(self) -> float:
        return self.fill_value / self.filled if self.filled > 0 else 0.0

    @property
    def done(self) -> bool:
        return self.status in DONE


class PositionTracker:
    """建玉・平均価格・確定損益を約定ごとに更新する"""

    def __init__(self) -> None:
        self.position = 0.0
        self.avg_price = 0.0
        self.realized = 0.0
        self.fees = 0.0
        self.open_t: Optional[float] = None
        self.round_trips: List[Tuple[float, float, float]] = []  # (時刻, 損益[円, 手数料込み], 保有秒)
        self._trip_pnl = 0.0
        self._trip_fees = 0.0

    def apply_fill(self, side: str, qty: float, price: float, fee: float, t: float) -> Optional[float]:
        """約定を反映。建玉がゼロに戻った（または反転した）ら、その往復の損益を返す。"""
        signed = qty if side == "BUY" else -qty
        pos = self.position
        self.fees += fee
        self._trip_fees += fee
        closed = None
        if abs(pos) < EPS or (pos > 0) == (signed > 0):
            new = round(pos + signed, 8)
            self.avg_price = (self.avg_price * abs(pos) + price * qty) / abs(new)
            self.position = new
            if abs(pos) < EPS:
                self.open_t = t
            return None
        close_qty = min(abs(pos), qty)
        pnl = close_qty * (price - self.avg_price) * (1.0 if pos > 0 else -1.0)
        self.realized += pnl
        self._trip_pnl += pnl
        new = round(pos + signed, 8)
        if abs(new) < EPS or (new > 0) != (pos > 0):
            closed = self._trip_pnl - self._trip_fees
            self.round_trips.append((t, closed, t - (self.open_t if self.open_t is not None else t)))
            self._trip_pnl = 0.0
            self._trip_fees = 0.0
            if abs(new) < EPS:
                self.position, self.avg_price, self.open_t = 0.0, 0.0, None
            else:  # 反転した分は新規建て
                self.position, self.avg_price, self.open_t = new, price, t
        else:
            self.position = new
        return closed

    def unrealized(self, bid: Optional[float], ask: Optional[float]) -> float:
        if abs(self.position) < EPS or bid is None or ask is None:
            return 0.0
        px = bid if self.position > 0 else ask  # 実際に決済できる側の価格で評価する
        return self.position * (px - self.avg_price)


def net_position(positions) -> Tuple[float, float]:
    """getpositions の結果を、符号付きの合計数量と平均価格にまとめる"""
    net, value = 0.0, 0.0
    for p in positions or []:
        try:
            sz = float(p.get("size", 0))
            px = float(p.get("price", 0))
        except (TypeError, ValueError):
            continue
        sgn = 1.0 if p.get("side") == "BUY" else -1.0
        net += sgn * sz
        value += sz * px
    gross = sum(float(p.get("size", 0) or 0) for p in positions or [])
    avg = value / gross if gross > 0 else 0.0
    return round(net, 8), avg


class _BrokerBase:
    live = False

    def __init__(self) -> None:
        self.pos = PositionTracker()
        self.orders: Dict[int, Order] = {}
        self._next_id = 1
        self.lock = threading.RLock()  # Engine が自分のロックに差し替える
        self.on_round_trip: Optional[Callable[[float, float], None]] = None
        self.order_count = 0
        self.fills: List[tuple] = []  # (時刻, side, 数量, 価格, 手数料, 目的, 理由, テイカー/メイカー)

    @property
    def position(self) -> float:
        return self.pos.position

    @property
    def avg_price(self) -> float:
        return self.pos.avg_price

    @property
    def position_open_t(self) -> Optional[float]:
        return self.pos.open_t

    def working_orders(self) -> List[Order]:
        return [o for o in self.orders.values() if not o.done]

    def _register(self, order: Order, t: float) -> Order:
        order.local_id = self._next_id
        self._next_id += 1
        order.created_t = t
        self.orders[order.local_id] = order
        self.order_count += 1
        if len(self.orders) > 2000:  # 終わった注文を捨てて辞書を小さく保つ
            for k in [k for k, o in self.orders.items() if o.done][:1000]:
                del self.orders[k]
        return order

    def _fill(self, o: Order, qty: float, price: float, fee: float, t: float, liq: str) -> None:
        qty = round(qty, 8)
        if qty <= 0:
            return
        o.filled = round(o.filled + qty, 8)
        o.fill_value += qty * price
        if o.remaining <= EPS:
            o.status = "FILLED"
        pnl = self.pos.apply_fill(o.side, qty, price, fee, t)
        self.fills.append((t, o.side, qty, price, fee, o.purpose, o.reason, liq))
        if pnl is not None and self.on_round_trip is not None:
            self.on_round_trip(pnl, t)


class PaperBroker(_BrokerBase):
    """模擬売買。約定は控えめ（自分に不利な側）に判定する:
    - IOC: 遅延のあと、その時の板を上から食う。指値を超える分は約定しない
    - GTC: 自分の価格より向こう側で約定が起きた（＝自分の価格の注文は全部消化された）時だけ約定
    """

    def __init__(self, capital: float, fee_rate: float = 0.0, latency_ms: float = 60.0,
                 leverage: float = 2.0, loss_cut_rate: float = 0.5,
                 leverage_point_rate_per_day: float = 0.0004):
        super().__init__()
        self.capital = float(capital)
        self.fee_rate = float(fee_rate)
        self.latency = float(latency_ms) / 1000.0
        self.leverage = float(leverage)
        self.loss_cut_rate = float(loss_cut_rate)
        self.lp_rate = float(leverage_point_rate_per_day)
        self.leverage_points = 0.0
        self.loss_cuts = 0
        self._roll_id: Optional[int] = None

    def start_equity(self) -> float:
        return self.capital

    def equity(self, bid: Optional[float], ask: Optional[float]) -> float:
        return (self.capital + self.pos.realized - self.pos.fees - self.leverage_points
                + self.pos.unrealized(bid, ask))

    def submit(self, order: Order, t: float) -> Order:
        self._register(order, t)
        order.sent_t = t
        order.active_t = t + self.latency
        order.status = "SENDING"
        return order

    def cancel(self, order: Order, t: float) -> None:
        if order.done or order.cancel_requested:
            return
        order.cancel_requested = True
        order.cancel_t = t + self.latency

    @staticmethod
    def _rollover_id(t: float) -> int:
        # 日本時間 18:00 を境に 1 日を数える（レバレッジポイントの模擬）
        return int(math.floor((t + 9 * 3600 - 18 * 3600) / 86400))

    def on_book(self, book, t: float) -> None:
        bid, ask = book.best_bid, book.best_ask
        rid = self._rollover_id(t)
        if self._roll_id is not None and rid != self._roll_id and abs(self.pos.position) > EPS and bid:
            self.leverage_points += abs(self.pos.position) * (bid + ask) / 2.0 * self.lp_rate
        self._roll_id = rid
        for o in [o for o in self.orders.values() if not o.done]:
            if o.status == "SENDING" and t >= o.active_t:
                if not self._margin_ok(o, bid, ask):
                    o.status, o.reject_reason = "REJECTED", "証拠金不足"
                    continue
                o.status = "ACTIVE"
                self._match_aggressive(o, book, t)
                if o.tif in ("IOC", "FOK") and not o.done:
                    o.status = "CANCELED"
            elif o.status == "ACTIVE" and o.tif == "GTC":
                # 板の反対側が自分の価格まで来た → メイカーとして約定
                if o.side == "BUY" and ask is not None and ask <= o.price:
                    self._fill(o, o.remaining, o.price, o.remaining * o.price * self.fee_rate, t, "maker")
                elif o.side == "SELL" and bid is not None and bid >= o.price:
                    self._fill(o, o.remaining, o.price, o.remaining * o.price * self.fee_rate, t, "maker")
            if (not o.done and o.cancel_requested and o.cancel_t is not None and t >= o.cancel_t):
                o.status = "CANCELED"
        self._check_loss_cut(book, t)

    def _margin_ok(self, o: Order, bid, ask) -> bool:
        if o.purpose != "entry" or bid is None:
            return True
        mid = (bid + ask) / 2.0
        signed = o.size if o.side == "BUY" else -o.size
        need = abs(self.pos.position + signed) * mid / self.leverage
        return need <= self.equity(bid, ask) + 1e-6

    def _match_aggressive(self, o: Order, book, t: float) -> None:
        levels = book.ask_levels() if o.side == "BUY" else book.bid_levels()
        for p, s in levels:
            if (o.side == "BUY" and p > o.price) or (o.side == "SELL" and p < o.price):
                break
            q = min(s, o.remaining)
            if q <= 0:
                break
            self._fill(o, q, p, q * p * self.fee_rate, t, "taker")
            if o.remaining <= EPS:
                break

    def on_execution(self, side: str, price: float, size: float, t: float) -> None:
        for o in self.orders.values():
            if o.done or o.status != "ACTIVE" or o.tif != "GTC":
                continue
            if o.side == "BUY" and side == "SELL" and price < o.price - EPS:
                self._fill(o, o.remaining, o.price, o.remaining * o.price * self.fee_rate, t, "maker")
            elif o.side == "SELL" and side == "BUY" and price > o.price + EPS:
                self._fill(o, o.remaining, o.price, o.remaining * o.price * self.fee_rate, t, "maker")

    def _check_loss_cut(self, book, t: float) -> None:
        pos = self.pos.position
        if abs(pos) < EPS:
            return
        bid, ask = book.best_bid, book.best_ask
        need = abs(pos) * (bid + ask) / 2.0 / self.leverage
        if need > 0 and self.equity(bid, ask) / need < self.loss_cut_rate:
            self.loss_cuts += 1
            self.force_close(book, t, reason="ロスカット（模擬）")

    def force_close(self, book, t: float, reason: str = "強制決済") -> None:
        for o in self.orders.values():
            if not o.done:
                o.status = "CANCELED"
        pos = self.pos.position
        if abs(pos) < EPS or book.best_bid is None:
            return
        side = "SELL" if pos > 0 else "BUY"
        px = book.best_bid if pos > 0 else book.best_ask
        o = self._register(Order(side=side, size=abs(pos), price=px, tif="IOC", purpose="exit",
                                 reason=reason), t)
        o.status = "ACTIVE"
        self._fill(o, abs(pos), px, abs(pos) * px * self.fee_rate, t, "taker")


class LiveBroker(_BrokerBase):
    """本番発注。発注・取消は専用スレッドで送り、約定は child_order_events で受け取る。"""

    live = True

    def __init__(self, rest: BitflyerREST, product_code: str, min_size: float = 0.001,
                 ioc_watchdog_sec: float = 5.0, gtc_watchdog_sec: float = 30.0,
                 reconcile_sec: float = 20.0, armed: bool = False):
        super().__init__()
        if not armed:
            raise RuntimeError("本番発注のロックが解除されていません")
        if product_code != "FX_BTC_JPY":
            raise RuntimeError("本番発注は FX_BTC_JPY（Crypto CFD）だけに対応しています。"
                               "現物は手数料が BTC で差し引かれ、建玉の扱いが異なるためです。")
        self.rest = rest
        self.pc = product_code
        self.min_size = min_size
        self.ioc_watchdog_sec = ioc_watchdog_sec
        self.gtc_watchdog_sec = gtc_watchdog_sec
        self.reconcile_sec = reconcile_sec
        self.by_acc: Dict[str, Order] = {}
        self._pending_events: Dict[str, list] = {}
        self._seen_exec: set = set()
        self._seen_exec_q: deque = deque()
        self._q: "queue.Queue" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self.base_collateral = 0.0
        self.exchange_collateral: Optional[float] = None
        self.needs_reconcile = False
        self._reconcile_queued = False
        self._last_reconcile = 0.0
        self._last_collateral = 0.0
        self.ack_ms: deque = deque(maxlen=500)
        self.risk = None  # Engine が設定（API エラーの数を数える）

    # ------------------------------------------------------------ 起動・停止
    def startup_check(self) -> dict:
        col = self.rest.get_collateral()
        positions = self.rest.get_positions(self.pc)
        active = self.rest.get_child_orders(self.pc, child_order_state="ACTIVE", count=10)
        net, _ = net_position(positions)
        if abs(net) > EPS:
            raise RuntimeError(f"既存の建玉 {net:+.8f} BTC があります。bitFlyer で決済してから起動してください。")
        if active:
            raise RuntimeError("未約定の注文が残っています。取り消してから起動してください。")
        self.base_collateral = float(col.get("collateral", 0) or 0)
        self.exchange_collateral = self.base_collateral
        return col

    def start(self) -> None:
        self._thread = threading.Thread(target=self._work, name="order-sender", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._q.put(("stop", None))
        if self._thread is not None:
            self._thread.join(timeout=10)

    def start_equity(self) -> float:
        return self.base_collateral

    def equity(self, bid: Optional[float], ask: Optional[float]) -> float:
        return self.base_collateral + self.pos.realized - self.pos.fees + self.pos.unrealized(bid, ask)

    # -------------------------------------------------------- 発注・取消
    def submit(self, order: Order, t: float) -> Order:
        with self.lock:
            self._register(order, t)
            order.status = "SENDING"
        self._q.put(("send", order))
        return order

    def cancel(self, order: Order, t: float) -> None:
        with self.lock:
            if order.done or order.cancel_requested:
                return
            order.cancel_requested = True
            order.cancel_t = t
            has_id = order.acceptance_id is not None
        if has_id:
            self._q.put(("cancel", order))
        # 受付IDがまだ無い時は、受付後に送る（_do_send の最後）

    def _note_error(self) -> None:
        if self.risk is not None:
            with self.lock:
                self.risk.note_api_error(time.time())

    def _work(self) -> None:
        while True:
            kind, obj = self._q.get()
            if kind == "stop":
                return
            try:
                if kind == "send":
                    self._do_send(obj)
                elif kind == "cancel":
                    self._do_cancel(obj)
                elif kind == "check":
                    self._do_check(obj)
                elif kind == "reconcile":
                    self._do_reconcile()
                elif kind == "collateral":
                    self._do_collateral()
                elif kind == "resolve_unknown":
                    self._do_resolve_unknown()
            except Exception:
                log.exception(f"注文スレッドでエラー（{kind}）")

    def _do_send(self, o: Order) -> None:
        t0 = time.time()
        try:
            res = self.rest.send_child_order(self.pc, o.side, o.size, "LIMIT", price=o.price,
                                             minute_to_expire=o.minute_to_expire, time_in_force=o.tif)
        except UncertainOrderError as e:
            log.error(f"発注の結果が不明: {e} → 全注文を取り消して建玉を照合します")
            with self.lock:
                o.status = "UNKNOWN"
                self.needs_reconcile = True
            self._note_error()
            self._q.put(("resolve_unknown", None))
            return
        except LocalRateLimit as e:
            with self.lock:
                o.status, o.reject_reason = "REJECTED", str(e)
            return
        except APIError as e:
            log.warning(f"発注を拒否されました: {e}")
            with self.lock:
                o.status, o.reject_reason = "REJECTED", str(e)
            self._note_error()
            return
        acc = res.get("child_order_acceptance_id") if isinstance(res, dict) else None
        with self.lock:
            o.sent_t = t0
            o.ack_ms = (time.time() - t0) * 1000.0
            self.ack_ms.append(o.ack_ms)
            if not acc:
                o.status = "UNKNOWN"
                self.needs_reconcile = True
                self._q.put(("resolve_unknown", None))
                return
            o.acceptance_id = acc
            self.by_acc[acc] = o
            if o.status == "SENDING":
                o.status = "ACTIVE"
            for ev in self._pending_events.pop(acc, []):  # 応答より先に届いていたイベント
                self._apply_event(o, ev, time.time())
            resend_cancel = o.cancel_requested and not o.done
        if resend_cancel:
            self._q.put(("cancel", o))

    def _do_cancel(self, o: Order) -> None:
        if not o.acceptance_id or o.done:
            return
        try:
            self.rest.cancel_child_order(self.pc, child_order_acceptance_id=o.acceptance_id)
        except APIError as e:
            log.info(f"取消できませんでした（約定済みの可能性）: {e}")

    # ------------------------------------------------------ 注文イベント
    def on_child_order_events(self, events, t: float) -> None:
        """child_order_events（配列）を反映する。Engine のロックの中で呼ばれる。"""
        for ev in events or []:
            if not isinstance(ev, dict) or ev.get("product_code") != self.pc:
                continue
            acc = ev.get("child_order_acceptance_id")
            o = self.by_acc.get(acc)
            if o is None:
                lst = self._pending_events.setdefault(acc, [])
                lst.append(ev)
                if len(self._pending_events) > 500:
                    self._pending_events.pop(next(iter(self._pending_events)))
                continue
            self._apply_event(o, ev, t)

    def _apply_event(self, o: Order, ev: dict, t: float) -> None:
        et = ev.get("event_type")
        if et == "ORDER":
            o.child_order_id = ev.get("child_order_id") or o.child_order_id
            if o.status == "SENDING":
                o.status = "ACTIVE"
        elif et == "ORDER_FAILED":
            o.status, o.reject_reason = "REJECTED", str(ev.get("reason", ""))
        elif et == "EXECUTION":
            exec_id = ev.get("exec_id")
            if exec_id is not None:
                if exec_id in self._seen_exec:
                    return
                self._seen_exec.add(exec_id)
                self._seen_exec_q.append(exec_id)
                if len(self._seen_exec_q) > 5000:
                    self._seen_exec.discard(self._seen_exec_q.popleft())
            try:
                qty = float(ev.get("size", 0))
                px = float(ev.get("price", 0))
                commission = float(ev.get("commission") or 0)
            except (TypeError, ValueError):
                return
            if o.filled + qty > o.size + 1e-9:  # REST の照合で反映済みの分
                return
            self._fill(o, qty, px, commission * px, t, "live")
            out = ev.get("outstanding_size")
            if out is not None:
                try:
                    if float(out) <= EPS:
                        o.status = "FILLED"
                except (TypeError, ValueError):
                    pass
        elif et == "CANCEL":
            if not o.done:
                o.status = "CANCELED"
        elif et == "EXPIRE":
            if not o.done:
                o.status = "EXPIRED"
        elif et == "CANCEL_FAILED":
            o.cancel_requested = False

    # -------------------------------------------------- 定期の確認・照合
    def housekeeping(self, now: float) -> None:
        with self.lock:
            for o in self.orders.values():
                if o.done or o.checking or o.status != "ACTIVE" or not o.acceptance_id:
                    continue
                limit = self.ioc_watchdog_sec if o.tif == "IOC" else self.gtc_watchdog_sec
                if now - o.sent_t > limit:
                    o.checking = True
                    self._q.put(("check", o))
            need = (self.needs_reconcile or now - self._last_reconcile > self.reconcile_sec)
            if need and not self._reconcile_queued:
                self._reconcile_queued = True
                self._q.put(("reconcile", None))
        if now - self._last_collateral > 30.0:
            self._last_collateral = now
            self._q.put(("collateral", None))

    def _do_check(self, o: Order) -> None:
        try:
            res = self.rest.get_child_orders(self.pc, child_order_acceptance_id=o.acceptance_id)
        except APIError as e:
            log.warning(f"注文の確認に失敗: {e}")
            with self.lock:
                o.checking = False
            return
        with self.lock:
            o.checking = False
            if o.done:
                return
            if not res:
                if time.time() - o.sent_t > 60:
                    o.status = "UNKNOWN"
                    self.needs_reconcile = True
                    self._q.put(("resolve_unknown", None))
                return
            r = res[0]
            executed = float(r.get("executed_size") or 0)
            avgp = float(r.get("average_price") or 0)
            if executed > o.filled + 1e-12:
                qty = round(executed - o.filled, 8)
                px = (avgp * executed - o.fill_value) / qty if qty > 0 else avgp
                log.warning(f"イベントの取りこぼしを補正: {o.side} {qty} @ {px:.0f}")
                self._fill(o, qty, px, 0.0, time.time(), "live")
            st = r.get("child_order_state")
            if st == "COMPLETED":
                o.status = "FILLED"
            elif st == "CANCELED":
                o.status = "CANCELED"
            elif st == "EXPIRED":
                o.status = "EXPIRED"
            elif st == "REJECTED":
                o.status = "REJECTED"

    def _do_reconcile(self, force: bool = False) -> None:
        try:
            positions = self.rest.get_positions(self.pc)
        except APIError as e:
            log.warning(f"建玉の取得に失敗: {e}")
            with self.lock:
                self._reconcile_queued = False
            return
        net, avg = net_position(positions)
        with self.lock:
            self._reconcile_queued = False
            self._last_reconcile = time.time()
            busy = any(o.status in ("SENDING", "ACTIVE") for o in self.orders.values())
            if busy and not force:
                return  # 注文の途中はずれていて当然なので見送る
            if abs(net - self.pos.position) > 1e-8:
                log.warning(f"建玉のずれを修正: 手元 {self.pos.position:+.8f} → 取引所 {net:+.8f}")
                self.pos.position = net
                self.pos.avg_price = avg if abs(net) > EPS else 0.0
                if abs(net) > EPS and self.pos.open_t is None:
                    self.pos.open_t = time.time()
                if abs(net) <= EPS:
                    self.pos.open_t = None
            for o in self.orders.values():
                if o.status == "UNKNOWN":
                    o.status = "CANCELED"
            self.needs_reconcile = False

    def _do_resolve_unknown(self) -> None:
        try:
            self.rest.cancel_all_child_orders(self.pc)
        except APIError as e:
            log.warning(f"全取消に失敗: {e}")
        time.sleep(1.5)
        self._do_reconcile(force=True)

    def _do_collateral(self) -> None:
        try:
            col = self.rest.get_collateral()
        except APIError as e:
            log.warning(f"証拠金の取得に失敗: {e}")
            return
        c = float(col.get("collateral", 0) or 0)
        with self.lock:
            self.exchange_collateral = c
            idle = abs(self.pos.position) < EPS and not any(not o.done for o in self.orders.values())
            if idle:
                local = self.base_collateral + self.pos.realized - self.pos.fees
                if abs(local - c) > 1.0:
                    log.info(f"証拠金を取引所の値に合わせます（手元 {local:,.0f} → 取引所 {c:,.0f}）")
                    self.base_collateral = c - (self.pos.realized - self.pos.fees)

    def flatten(self, get_bid_ask: Callable[[], Tuple[Optional[float], Optional[float]]],
                tries: int = 5) -> bool:
        """全注文を取り消し、建玉を IOC で決済する（終了時・停止時）。REST を直接呼ぶ。"""
        try:
            self.rest.cancel_all_child_orders(self.pc)
        except APIError as e:
            log.warning(f"全取消に失敗: {e}")
        time.sleep(1.0)
        for _ in range(tries):
            try:
                net, avg = net_position(self.rest.get_positions(self.pc))
            except APIError as e:
                log.warning(f"建玉の取得に失敗: {e}")
                time.sleep(1.0)
                continue
            with self.lock:
                if abs(net - self.pos.position) > 1e-8:
                    log.warning(f"建玉のずれを修正: 手元 {self.pos.position:+.8f} → 取引所 {net:+.8f}")
                    self.pos.position = net
                    self.pos.avg_price = avg if abs(net) > EPS else 0.0
                    self.pos.open_t = time.time() if abs(net) > EPS else None
            if abs(net) < EPS:
                log.info("建玉はありません")
                return True
            bid, ask = get_bid_ask()
            if bid is None or ask is None:
                try:
                    tk = self.rest.get_ticker(self.pc)
                    bid, ask = float(tk["best_bid"]), float(tk["best_ask"])
                except (APIError, KeyError, TypeError, ValueError):
                    time.sleep(1.0)
                    continue
            size = abs(net)
            if size < self.min_size - EPS:
                side = "BUY" if net > 0 else "SELL"   # 端数は同方向に足してから決済
                size = self.min_size
            else:
                side = "SELL" if net > 0 else "BUY"
            px = math.floor(bid * 0.997) if side == "SELL" else math.ceil(ask * 1.003)
            try:
                res = self.rest.send_child_order(self.pc, side, round(size, 8), "LIMIT", price=px,
                                                 time_in_force="IOC")
                log.info(f"決済注文: {side} {size:.8f} @ {px}")
            except (APIError, UncertainOrderError) as e:
                log.warning(f"決済注文に失敗: {e}")
                time.sleep(1.5)
                continue
            acc = res.get("child_order_acceptance_id") if isinstance(res, dict) else None
            if acc:
                # 約定イベントで損益を記録できるよう、この注文も管理下に置く
                with self.lock:
                    o = self._register(Order(side=side, size=round(size, 8), price=px, tif="IOC",
                                             purpose="exit", reason="一括決済"), time.time())
                    o.status, o.acceptance_id, o.sent_t = "ACTIVE", acc, time.time()
                    self.by_acc[acc] = o
                    for ev in self._pending_events.pop(acc, []):
                        self._apply_event(o, ev, time.time())
                deadline = time.time() + 2.0
                while time.time() < deadline and not o.done:
                    time.sleep(0.05)
                if not o.done:
                    self._do_check(o)  # イベントが来なければ REST で約定を確かめる
            time.sleep(0.5)
        log.error("建玉を決済しきれませんでした。bitFlyer の画面で確認してください。")
        return False
