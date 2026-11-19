"""全体をつなぐ部分（リアルタイム／ペーパー／本番／バックテストで同じ処理を通す）"""

from __future__ import annotations

import csv
import logging
import os
import signal
import threading
import time
from collections import deque
from typing import List, Optional

from .book import OrderBook
from .broker import EPS, LiveBroker, PaperBroker
from .config import make_dc
from .features import FeatureConfig, FeatureEngine
from .risk import RiskConfig, RiskManager
from .strategy import Strategy, StrategyConfig
from .util import iter_records, jst_str, parse_exec_date

log = logging.getLogger("bfbot")


def pct(values, q: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    k = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[k]


class Engine:
    def __init__(self, cfg: dict, broker, mode: str = "paper", quiet: bool = False):
        self.cfg = cfg
        self.mode = mode
        self.quiet = quiet
        self.pc = cfg["product_code"]
        self.lock = threading.RLock()
        self.book = OrderBook()
        self.feat = FeatureEngine(make_dc(FeatureConfig, cfg["features"]))
        self.strategy = Strategy(make_dc(StrategyConfig, cfg["strategy"]))
        self.broker = broker
        broker.lock = self.lock
        self.risk = RiskManager(make_dc(RiskConfig, cfg["risk"]), start_equity=broker.start_equity(),
                                require_exchange_status=(mode == "live"))
        if isinstance(broker, LiveBroker):
            broker.risk = self.risk
        broker.on_round_trip = self._on_round_trip
        self.ch_snap = "lightning_board_snapshot_" + self.pc
        self.ch_board = "lightning_board_" + self.pc
        self.ch_exec = "lightning_executions_" + self.pc
        self.report_sec = float(cfg.get("report_sec", 10.0))
        self._last_report = 0.0
        self.lag_ms: deque = deque(maxlen=5000)
        self.proc_us: deque = deque(maxlen=20000)
        self.msg_count = 0
        self.trading_enabled = True
        self.last_features: Optional[dict] = None
        self.equity_curve: List[tuple] = []
        self._last_eq_sample = 0.0
        self.blocked: dict = {}
        self.first_t: Optional[float] = None
        self.last_t: Optional[float] = None
        self.last_equity: Optional[float] = None

    def channels(self) -> List[str]:
        return [self.ch_snap, self.ch_board, self.ch_exec]

    def on_disconnect(self) -> None:
        with self.lock:
            self.book.reset()
            self.feat.reset_flow()

    # ------------------------------------------------------------ 受信処理
    def on_message(self, channel: str, message, t: float) -> None:
        t0 = time.perf_counter()
        with self.lock:
            self.msg_count += 1
            if self.first_t is None:
                self.first_t = t
            self.last_t = t
            if channel == self.ch_board:
                self.book.apply_diff(message, t)
                self.feat.on_book(self.book, t)
            elif channel == self.ch_snap:
                self.book.apply_snapshot(message, t)
                self.feat.on_book(self.book, t)
            elif channel == self.ch_exec:
                for e in message or ():
                    try:
                        side = e.get("side") or ""
                        price = float(e["price"])
                        size = float(e["size"])
                    except (AttributeError, KeyError, TypeError, ValueError):
                        continue
                    self.feat.on_execution(side, size, price, t)
                    if not self.broker.live:
                        self.broker.on_execution(side, price, size, t)
                    ed = e.get("exec_date")
                    if ed and self.mode != "backtest":
                        self.lag_ms.append((t - parse_exec_date(ed)) * 1000.0)
            elif channel == "child_order_events":
                if self.broker.live:
                    self.broker.on_child_order_events(message, t)
            elif channel == "_disconnect":
                self.book.reset()
                self.feat.reset_flow()
                return
            else:
                return
            self._step(t)
        self.proc_us.append((time.perf_counter() - t0) * 1e6)

    def _step(self, t: float) -> None:
        book = self.book
        if not book.ready:
            return
        if not self.broker.live:
            self.broker.on_book(book, t)
        bid, ask = book.best_bid, book.best_ask
        eq = self.broker.equity(bid, ask)
        self.last_equity = eq
        self.risk.update_equity(eq, t)
        if t - self._last_eq_sample >= 1.0:
            self.equity_curve.append((t, eq))
            self._last_eq_sample = t
        f = self.feat.compute(book, t)
        self.last_features = f
        if self.trading_enabled:
            for kind, obj in self.strategy.decide(f, book, self.broker, self.risk, t):
                if kind == "cancel":
                    self.broker.cancel(obj, t)
                    continue
                is_entry = obj.purpose == "entry"
                ok, why = self.risk.order_allowed(t, is_exit=not is_entry)
                if ok and is_entry:
                    signed = obj.size if obj.side == "BUY" else -obj.size
                    if abs(self.broker.position + signed) > self.risk.cfg.max_position_btc + EPS:
                        ok, why = False, "建玉上限"
                if not ok:
                    self.blocked[why] = self.blocked.get(why, 0) + 1
                    continue
                self.broker.submit(obj, t)
                self.risk.note_order(t)
                self.strategy.on_submitted(obj, t)
                if not self.quiet:
                    log.info(f"注文 {obj.purpose}:{obj.reason} {obj.side} {obj.size} @ {obj.price:,.0f} "
                             f"{obj.tif} | 最良 {bid:,.0f}/{ask:,.0f}")
        if not self.quiet and t - self._last_report >= self.report_sec:
            self._last_report = t
            self._report(t, f, eq)

    def _on_round_trip(self, pnl: float, t: float) -> None:
        self.risk.on_round_trip(pnl, t)
        if not self.quiet:
            n = len(self.broker.pos.round_trips)
            log.info(f"決済 #{n}: {pnl:+,.1f} 円")

    def _report(self, t: float, f: dict, eq: float) -> None:
        rts = self.broker.pos.round_trips
        wins = sum(1 for _, p, _ in rts if p > 0)
        wr = f"{wins / len(rts) * 100:.0f}%" if rts else "-"
        vol = f.get("vol_bps_1m")
        lag = pct(list(self.lag_ms)[-500:], 0.5)
        proc = pct(list(self.proc_us)[-2000:], 0.5)
        log.info(f"資産 {eq:,.0f} 円 | 建玉 {self.broker.position:+.3f} | 往復 {len(rts)} 勝率 {wr} | "
                 f"score {f['score']:+.2f} spread {f['spread_bps']:.2f}bps "
                 f"vol {vol if vol is None else round(vol, 1)}bps/分 | "
                 f"受信遅れ {lag if lag is None else round(lag)}ms 処理 {proc if proc is None else round(proc)}µs"
                 + (f" | 停止: {self.risk.halt_reason}" if self.risk.halted else ""))

    # ------------------------------------------------------------ 集計
    def summary(self) -> dict:
        rts = self.broker.pos.round_trips
        pnls = [p for _, p, _ in rts]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        start = self.broker.start_equity()
        end = self.last_equity if self.last_equity is not None else start
        peak, mdd = start, 0.0
        for _, e in self.equity_curve:
            peak = max(peak, e)
            mdd = max(mdd, peak - e)
        hours = ((self.last_t - self.first_t) / 3600.0) if self.first_t and self.last_t else 0.0
        size = float(self.cfg["strategy"]["size"])
        mid = self.book.mid or 0.0
        avg_bps = (sum(pnls) / len(pnls) / (size * mid) * 1e4) if pnls and mid > 0 and size > 0 else None
        ack = list(getattr(self.broker, "ack_ms", []))
        return {
            "期間_時間": hours,
            "往復数": len(rts),
            "勝率": (len(wins) / len(rts)) if rts else None,
            "損益合計_円": sum(pnls),
            "1往復平均_円": (sum(pnls) / len(pnls)) if pnls else None,
            "1往復平均_bps": avg_bps,
            "平均利益_円": (sum(wins) / len(wins)) if wins else None,
            "平均損失_円": (sum(losses) / len(losses)) if losses else None,
            "PF": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None,
            "最大ドローダウン_円": mdd,
            "開始資産_円": start,
            "終了資産_円": end,
            "倍率": (end / start) if start else None,
            "注文数": self.broker.order_count,
            "手数料_円": self.broker.pos.fees,
            "決済理由": dict(self.strategy.exit_reasons),
            "見送り": dict(self.blocked),
            "停止理由": self.risk.halt_reason,
            "処理時間_中央値_µs": pct(list(self.proc_us), 0.5),
            "処理時間_99%_µs": pct(list(self.proc_us), 0.99),
            "受信遅れ_中央値_ms": pct(list(self.lag_ms), 0.5),
            "発注応答_中央値_ms": pct(ack, 0.5),
            "発注応答_90%_ms": pct(ack, 0.9),
        }

    def print_summary(self) -> None:
        s = self.summary()
        print("\n========== 結果 ==========")
        for k, v in s.items():
            if isinstance(v, float):
                if k == "勝率":
                    print(f"{k:16s}: {v * 100:.1f}%")
                elif k == "倍率":
                    print(f"{k:16s}: {v:.4f}")
                else:
                    print(f"{k:16s}: {v:,.2f}")
            else:
                print(f"{k:16s}: {v}")

    def save_trades(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["日時(JST)", "売買", "数量", "価格", "手数料", "目的", "理由", "種別"])
            for t, side, q, p, fee, purpose, reason, liq in self.broker.fills:
                w.writerow([jst_str(t), side, q, p, round(fee, 4), purpose, reason, liq])


# ------------------------------------------------------------------ 実行
def make_paper_broker(cfg: dict) -> PaperBroker:
    p = cfg["paper"]
    return PaperBroker(capital=cfg["capital_jpy"], fee_rate=p["fee_rate"], latency_ms=p["latency_ms"],
                       leverage=p["leverage"], loss_cut_rate=p["loss_cut_rate"],
                       leverage_point_rate_per_day=p["leverage_point_rate_per_day"])


def run_backtest(cfg: dict, paths, quiet: bool = True, t_from: Optional[float] = None,
                 t_to: Optional[float] = None) -> Engine:
    """t_from より前は板と指標を作るだけ（売買しない）。t_to 以降は読まない。"""
    broker = make_paper_broker(cfg)
    eng = Engine(cfg, broker, mode="backtest", quiet=quiet)
    warming = t_from is not None
    if warming:
        eng.trading_enabled = False
    for rec in iter_records(paths):
        ch = rec.get("ch")
        t = rec.get("t")
        if ch is None or t is None:
            continue
        if t_to is not None and t >= t_to:
            break
        if warming and t >= t_from:
            warming = False
            eng.trading_enabled = True
            eng.first_t = None
            eng.equity_curve.clear()
        eng.on_message(ch, rec.get("m"), float(t))
    if eng.book.ready and eng.last_t is not None:
        broker.force_close(eng.book, eng.last_t, reason="バックテスト終了")
        eng.last_equity = broker.equity(eng.book.best_bid, eng.book.best_ask)
    return eng


def run_realtime(cfg: dict, mode: str, minutes: Optional[float] = None,
                 api_key: str = "", api_secret: str = "", armed: bool = False,
                 ws_url: Optional[str] = None, rest_base: str = "https://api.bitflyer.com") -> Engine:
    from .rest import APIError, BitflyerREST
    from .ws import RealtimeClient

    pc = cfg["product_code"]
    rest = BitflyerREST(api_key=api_key if mode == "live" else "",
                        api_secret=api_secret if mode == "live" else "", base_url=rest_base)
    private = None
    if mode == "live":
        broker = LiveBroker(rest, pc, min_size=float(cfg["strategy"]["min_size"]), armed=armed)
        col = broker.startup_check()
        log.info(f"本番: 証拠金 {float(col.get('collateral', 0)):,.0f} 円 / 維持率 {col.get('keep_rate')}")
    else:
        broker = make_paper_broker(cfg)
    eng = Engine(cfg, broker, mode=mode)
    public = RealtimeClient(eng.channels(), eng.on_message, url=ws_url, name="market",
                            on_disconnect=eng.on_disconnect)
    if mode == "live":
        private = RealtimeClient(["child_order_events"], eng.on_message, url=ws_url, api_key=api_key,
                                 api_secret=api_secret, name="orders", stale_sec=600.0)
        private.start()
        if not private.connected.wait(20):
            private.stop()
            raise RuntimeError("注文イベント（Private Channel）に接続できません。APIキーの権限"
                               "「注文のイベントを受信」を確認してください。")
        broker.start()
    public.start()

    stop = threading.Event()

    def _sig(*_):
        stop.set()

    old_int = signal.signal(signal.SIGINT, _sig)
    old_term = signal.signal(signal.SIGTERM, _sig)
    t_end = time.time() + minutes * 60.0 if minutes else None
    last_state = 0.0
    stale_since: Optional[float] = None
    log.info(f"開始: {mode} / {pc}（Ctrl+C で終了）")
    try:
        while not stop.is_set():
            stop.wait(1.0)
            now = time.time()
            if t_end and now >= t_end:
                break
            if now - last_state >= 10.0:  # 取引所の状態確認（HTTPS 接続の維持も兼ねる）
                last_state = now
                try:
                    st = rest.get_boardstate(pc)
                    with eng.lock:
                        eng.risk.set_exchange_status(st.get("health", "?"), st.get("state", "?"))
                except APIError as e:
                    log.warning(f"板の状態を取得できません: {e}")
                    if mode == "live":
                        with eng.lock:
                            eng.risk.set_exchange_status("不明", "不明")
            if mode == "live":
                broker.housekeeping(now)
                with eng.lock:
                    has_pos = abs(broker.position) > EPS
                    stale = (not eng.book.ready) or (now - eng.book.last_update > 15.0)
                    halted = eng.risk.halted
                if has_pos and (stale or halted):
                    stale_since = stale_since or now
                    if now - stale_since > 10.0:  # データが止まった・停止したのに建玉が残る
                        log.error("データ停止または停止状態で建玉が残っているため、REST で決済します")
                        broker.flatten(lambda: (eng.book.best_bid, eng.book.best_ask))
                        stale_since = None
                else:
                    stale_since = None
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)
        with eng.lock:
            eng.trading_enabled = False
        if mode == "live":
            broker.stop()
            if cfg["live"].get("flatten_on_exit", True):
                broker.flatten(lambda: (eng.book.best_bid, eng.book.best_ask))
            if private is not None:
                private.stop()
        else:
            with eng.lock:
                if eng.book.ready and eng.last_t is not None:
                    broker.force_close(eng.book, eng.last_t, reason="ペーパー終了")
                    eng.last_equity = broker.equity(eng.book.best_bid, eng.book.best_ask)
        public.stop()
        rest.close()
    return eng
