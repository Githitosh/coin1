"""板・指標・建玉計算・模擬約定・本番ブローカー・全体の流れのテスト"""

import math
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bfbot.book import OrderBook  # noqa: E402
from bfbot.broker import LiveBroker, Order, PaperBroker, PositionTracker, net_position  # noqa: E402
from bfbot.config import load_config  # noqa: E402
from bfbot.engine import run_backtest, run_realtime  # noqa: E402
from bfbot.features import FeatureConfig, FeatureEngine  # noqa: E402
from bfbot.rest import UncertainOrderError  # noqa: E402
from bfbot.synth import generate  # noqa: E402
from bfbot import target  # noqa: E402
from tests.fake_exchange import KEY, SECRET, FakeExchange  # noqa: E402


def lv(*pairs):
    return [{"price": p, "size": s} for p, s in pairs]


class TestBook(unittest.TestCase):
    def test_snapshot_diff(self):
        b = OrderBook()
        b.apply_snapshot({"bids": lv((100, 1), (99, 2), (98, 3)), "asks": lv((102, 1), (101, 2))}, 1.0)
        self.assertTrue(b.ready)
        self.assertEqual((b.best_bid, b.best_ask, b.mid, b.spread), (100, 101, 100.5, 1))
        b.apply_diff({"bids": lv((100, 0), (99, 5)), "asks": lv((101, 0), (103, 1), (0, 9))}, 2.0)
        self.assertEqual((b.best_bid, b.best_ask), (99, 102))
        self.assertEqual(b.bids[99], 5)
        self.assertNotIn(0, b.asks)                     # 板寄せの price 0 は載せない
        self.assertAlmostEqual(b.imbalance(2), (5 + 3 - 1 - 1) / (5 + 3 + 1 + 1))
        self.assertAlmostEqual(b.microprice(), (99 * 1 + 102 * 5) / 6)

    def test_crossed_book_is_repaired(self):
        b = OrderBook()
        b.apply_snapshot({"bids": lv((100, 1)), "asks": lv((101, 1), (102, 1))}, 1.0)
        b.apply_diff({"bids": lv((101.5, 2))}, 2.0)    # 新しい買いが古い売り 101 を越えた
        self.assertEqual(b.best_bid, 101.5)
        self.assertEqual(b.best_ask, 102)
        self.assertLess(b.best_bid, b.best_ask)


class TestFeatures(unittest.TestCase):
    def test_uptrend_and_buy_flow_give_positive_score(self):
        fe = FeatureEngine(FeatureConfig(warmup_sec=5, regime_warmup_sec=10))
        b = OrderBook()
        t, mid = 0.0, 1_000_000.0
        for i in range(600):
            t += 0.1
            mid += 3 if i > 300 else (1 if i % 2 else -1)
            b.apply_snapshot({"bids": lv((mid - 1, 2.0), (mid - 2, 1)), "asks": lv((mid + 1, 0.5), (mid + 2, 1))}, t)
            fe.on_book(b, t)
            if i > 300:
                fe.on_execution("BUY", 0.1, mid + 1, t)
        f = fe.compute(b, t)
        self.assertTrue(f["warm"])
        self.assertGreater(f["trend_z"], 0)
        self.assertGreater(f["flow"], 0.9)
        self.assertGreater(f["obi"], 0)
        self.assertGreater(f["score"], 0.3)


class TestPosition(unittest.TestCase):
    def test_round_trip_and_flip(self):
        p = PositionTracker()
        self.assertIsNone(p.apply_fill("BUY", 0.002, 100.0, 0.0, 1))
        self.assertIsNone(p.apply_fill("BUY", 0.002, 110.0, 0.0, 2))
        self.assertAlmostEqual(p.avg_price, 105.0)
        self.assertIsNone(p.apply_fill("SELL", 0.001, 120.0, 0.0, 3))     # 一部決済
        self.assertAlmostEqual(p.position, 0.003)
        pnl = p.apply_fill("SELL", 0.005, 100.0, 0.0, 4)                  # 決済して反転
        self.assertAlmostEqual(pnl, 0.001 * 15 + 0.003 * -5)
        self.assertAlmostEqual(p.position, -0.002)
        self.assertAlmostEqual(p.avg_price, 100.0)
        pnl = p.apply_fill("BUY", 0.002, 90.0, 0.5, 5)
        self.assertAlmostEqual(pnl, 0.002 * 10 - 0.5)
        self.assertEqual(p.position, 0.0)
        self.assertEqual(len(p.round_trips), 2)

    def test_net_position(self):
        net, avg = net_position([{"side": "BUY", "price": 100, "size": 0.002},
                                 {"side": "BUY", "price": 110, "size": 0.002}])
        self.assertAlmostEqual(net, 0.004)
        self.assertAlmostEqual(avg, 105)


class TestPaperBroker(unittest.TestCase):
    def book(self):
        b = OrderBook()
        b.apply_snapshot({"bids": lv((1000, 0.001), (999, 0.002)), "asks": lv((1001, 0.001), (1002, 0.001), (1005, 5))}, 0)
        return b

    def test_ioc_walks_book_and_respects_limit(self):
        br = PaperBroker(100000, latency_ms=50)
        b = self.book()
        o = br.submit(Order("BUY", 0.003, 1002, tif="IOC"), 0.0)
        br.on_book(b, 0.01)
        self.assertEqual(o.status, "SENDING")            # まだ届いていない（遅延）
        br.on_book(b, 0.06)
        self.assertEqual(o.status, "CANCELED")           # 1002 までしか買わず、残りは取消
        self.assertAlmostEqual(o.filled, 0.002)
        self.assertAlmostEqual(o.avg_fill, 1001.5)

    def test_maker_fills_only_on_trade_through(self):
        br = PaperBroker(100000, latency_ms=0)
        b = self.book()
        o = br.submit(Order("BUY", 0.001, 1000, tif="GTC"), 0.0)
        br.on_book(b, 0.0)
        br.on_execution("SELL", 1000, 0.5, 0.1)          # 同じ価格の約定では約定扱いにしない
        self.assertEqual(o.status, "ACTIVE")
        br.on_execution("SELL", 999, 0.001, 0.2)         # 向こう側で約定 → 自分の価格は消化済み
        self.assertEqual(o.status, "FILLED")
        self.assertAlmostEqual(br.position, 0.001)

    def test_margin_and_fee(self):
        br = PaperBroker(1000, fee_rate=0.0015, latency_ms=0, leverage=2)
        b = self.book()
        o = br.submit(Order("BUY", 5.0, 2000, tif="IOC"), 0.0)
        br.on_book(b, 0.0)
        self.assertEqual(o.status, "REJECTED")           # 証拠金不足
        o = br.submit(Order("BUY", 0.001, 1001, tif="IOC"), 1.0)
        br.on_book(b, 1.0)
        self.assertAlmostEqual(br.pos.fees, 0.001 * 1001 * 0.0015)


class FakeRest:
    """LiveBroker 単体テスト用。通信の失敗も起こせる。"""

    def __init__(self):
        self.sent = []
        self.cancel_all = 0
        self.positions = []
        self.fail_next = False
        self.n = 0

    def get_collateral(self):
        return {"collateral": 100000, "keep_rate": 0}

    def get_positions(self, pc):
        return self.positions

    def get_child_orders(self, pc, **kw):
        return []

    def send_child_order(self, pc, side, size, t, price=None, minute_to_expire=None, time_in_force=None):
        if self.fail_next:
            self.fail_next = False
            raise UncertainOrderError("cut")
        self.n += 1
        self.sent.append((side, size, price, time_in_force))
        return {"child_order_acceptance_id": f"JRF-{self.n}"}

    def cancel_child_order(self, pc, **kw):
        return None

    def cancel_all_child_orders(self, pc):
        self.cancel_all += 1

    def get_ticker(self, pc):
        return {"best_bid": 1000, "best_ask": 1001}


class TestLiveBroker(unittest.TestCase):
    def test_locks(self):
        with self.assertRaises(RuntimeError):
            LiveBroker(FakeRest(), "FX_BTC_JPY")                    # ロック解除なし
        with self.assertRaises(RuntimeError):
            LiveBroker(FakeRest(), "BTC_JPY", armed=True)           # 現物は本番非対応
        r = FakeRest()
        r.positions = [{"side": "BUY", "price": 1, "size": 0.01}]
        with self.assertRaises(RuntimeError):
            LiveBroker(r, "FX_BTC_JPY", armed=True).startup_check()  # 既存の建玉があると起動しない

    def wait(self, cond, sec=3):
        deadline = time.time() + sec
        while time.time() < deadline:
            if cond():
                return True
            time.sleep(0.01)
        return False

    def test_event_before_ack_and_dedupe(self):
        r = FakeRest()
        br = LiveBroker(r, "FX_BTC_JPY", armed=True)
        br.startup_check()
        # 受付IDの応答より先に約定イベントが届く場合
        br.on_child_order_events([{"product_code": "FX_BTC_JPY", "child_order_acceptance_id": "JRF-1",
                                   "event_type": "EXECUTION", "exec_id": 7, "side": "BUY", "price": 1001,
                                   "size": 0.001, "commission": 0, "outstanding_size": 0}], 1.0)
        br.start()
        o = br.submit(Order("BUY", 0.001, 1002, tif="IOC"), 1.0)
        self.assertTrue(self.wait(lambda: o.status == "FILLED"))
        self.assertAlmostEqual(br.position, 0.001)
        br.on_child_order_events([{"product_code": "FX_BTC_JPY", "child_order_acceptance_id": "JRF-1",
                                   "event_type": "EXECUTION", "exec_id": 7, "side": "BUY", "price": 1001,
                                   "size": 0.001, "outstanding_size": 0}], 1.1)
        self.assertAlmostEqual(br.position, 0.001)                  # 同じ約定は二重に数えない
        br.stop()

    def test_uncertain_order_cancels_all_and_reconciles(self):
        r = FakeRest()
        br = LiveBroker(r, "FX_BTC_JPY", armed=True)
        br.startup_check()
        br.start()
        r.fail_next = True
        r.positions = [{"side": "BUY", "price": 1001, "size": 0.001}]  # 実は受け付けられて約定していた
        o = br.submit(Order("BUY", 0.001, 1002, tif="IOC"), 1.0)
        self.assertTrue(self.wait(lambda: o.done and r.cancel_all == 1, 5))
        self.assertAlmostEqual(br.position, 0.001)                  # 取引所の建玉に合わせた
        self.assertEqual(len(br.working_orders()), 0)
        br.stop()


class TestBacktestAndRisk(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.path = os.path.join(cls.tmp, "s.jsonl.gz")
        generate(cls.path, minutes=25, seed=3)

    def test_backtest_runs_and_is_flat_at_end(self):
        cfg = load_config(None)
        eng = run_backtest(cfg, [self.path])
        s = eng.summary()
        self.assertGreater(s["往復数"], 5)
        self.assertAlmostEqual(eng.broker.position, 0.0)
        self.assertLessEqual(max(abs(f[2]) for f in eng.broker.fills), 0.002 + 1e-9)

    def test_daily_loss_limit_halts_and_flattens(self):
        cfg = load_config(None, {"risk": {"max_daily_loss_jpy": 1.0, "max_drawdown_jpy": 1.0},
                                 "strategy": {"entry_threshold": 0.05, "take_profit_bps": 1000,
                                              "stop_loss_bps": 1000, "max_hold_sec": 1000,
                                              "exit_on_reverse": 2.0}})
        eng = run_backtest(cfg, [self.path])
        self.assertTrue(eng.risk.halted)
        self.assertIn("停止", eng.strategy.exit_reasons)
        n_after = [f for f in eng.broker.fills if f[5] == "entry"]
        self.assertLessEqual(len(n_after), 3)


class TestTarget(unittest.TestCase):
    def test_numbers(self):
        rr = target.required_rates(100, 3)
        self.assertAlmostEqual(rr["1日あたり"], 100 ** (1 / 3))
        self.assertAlmostEqual((rr["1時間あたり"] - 1) * 100, 6.6, places=1)
        self.assertAlmostEqual(target.hold_price_multiple(100, 2), 50.5)
        self.assertAlmostEqual(target.all_win_multiple(5, 500, 3, 2), 1.001 ** 1500)
        need = target.required_capture_bps(100, 2000, 3, 2)
        self.assertAlmostEqual((1 + 2 * need / 1e4) ** 6000, 100, places=6)
        r = target.prob_reach(0.55, 5, 5, 2000, 3, 2)
        self.assertAlmostEqual(r["必要な勝率"], 0.884, places=3)
        self.assertEqual(r["100倍に届く確率"], 0.0)
        self.assertLess(r["100倍に届く確率_log"] / math.log(10), -600)
        # 小さい n で総当たりと一致するか
        n, p = 10, 0.6
        r = target.prob_reach(p, 3000, 2000, n, 1, 1, multiple=2.0)
        brute = sum(math.comb(n, k) * p ** k * (1 - p) ** (n - k) for k in range(n + 1)
                    if 1.3 ** k * 0.8 ** (n - k) >= 2.0)
        self.assertAlmostEqual(r["100倍に届く確率"], brute, places=12)

    def test_perfect_foresight_simple(self):
        lg, hrs = target.perfect_foresight([0, 1, 2, 3], [100, 110, 99, 99], 1.0, 1.0, 0.0)
        self.assertAlmostEqual(lg, math.log(1.1) + math.log(1 + 11 / 110))


class TestIntegrationWithFakeExchange(unittest.TestCase):
    """偽取引所（ローカル）で、ペーパーと本番の流れを最後まで通す"""

    @classmethod
    def setUpClass(cls):
        cls.ex = FakeExchange().start()

    @classmethod
    def tearDownClass(cls):
        cls.ex.stop()

    def fast_cfg(self):
        return load_config(None, {
            "features": {"warmup_sec": 1.0, "regime_warmup_sec": 9999},
            "strategy": {"entry_threshold": 0.01, "max_hold_sec": 0.6, "cooldown_sec": 0.3,
                         "min_vol_bps_1m": 0.0, "max_vol_bps_1m": 1e9, "max_spread_bps": 100},
            "risk": {"min_order_interval_sec": 0.2, "max_orders_per_min": 200},
            "report_sec": 3600})

    def test_paper_realtime(self):
        eng = run_realtime(self.fast_cfg(), "paper", minutes=0.12, ws_url=self.ex.ws_url,
                           rest_base=self.ex.rest_base)
        self.assertGreater(eng.msg_count, 50)
        self.assertGreater(eng.broker.order_count, 1)
        self.assertAlmostEqual(eng.broker.position, 0.0)

    def test_live_end_to_end(self):
        n0, bad0 = self.ex.n_orders, self.ex.bad_sigs
        eng = run_realtime(self.fast_cfg(), "live", minutes=0.2, api_key=KEY, api_secret=SECRET, armed=True,
                           ws_url=self.ex.ws_url, rest_base=self.ex.rest_base)
        s = eng.summary()
        self.assertEqual(self.ex.bad_sigs, bad0)               # 署名はすべて正しい
        self.assertGreater(self.ex.n_orders - n0, 3)           # 実際に発注した
        self.assertAlmostEqual(self.ex.position, 0.0)          # 終了時に取引所側の建玉はゼロ
        self.assertAlmostEqual(eng.broker.position, 0.0)
        self.assertGreater(s["往復数"], 0)
        self.assertGreaterEqual(self.ex.cancel_all_calls, 1)   # 終了時に全取消
        self.assertAlmostEqual(eng.broker.pos.realized, self.ex.realized, places=6)  # 損益が一致

    def test_live_flatten_on_exit_keeps_pnl(self):
        cfg = self.fast_cfg()
        cfg["strategy"].update({"max_hold_sec": 9999, "take_profit_bps": 9999, "stop_loss_bps": 9999,
                                "exit_on_reverse": 2.0})
        r0 = self.ex.realized
        eng = run_realtime(cfg, "live", minutes=0.08, api_key=KEY, api_secret=SECRET, armed=True,
                           ws_url=self.ex.ws_url, rest_base=self.ex.rest_base)
        self.assertTrue(any(f[6] == "一括決済" for f in eng.broker.fills))  # 終了時の決済が走った
        self.assertAlmostEqual(self.ex.position, 0.0)
        self.assertAlmostEqual(eng.broker.position, 0.0)
        self.assertAlmostEqual(eng.broker.pos.realized, self.ex.realized - r0, places=6)


if __name__ == "__main__":
    unittest.main()


class TestRealSim(unittest.TestCase):
    def test_walk_forward_report(self):
        from bfbot import realsim
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "s.jsonl.gz")
        generate(path, minutes=14, seed=5)
        import gzip
        import json as _json
        last_t = None
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                last_t = _json.loads(line)["t"]
        with gzip.open(path, "at", encoding="utf-8") as fh:  # 記録の最後に付く切断の目印
            fh.write(_json.dumps({"t": last_t + 0.1, "ch": "_disconnect", "m": None}) + "\n")
        old = realsim.GRID
        realsim.GRID = old[:2]
        try:
            out = os.path.join(tmp, "site")
            res = realsim.run([path], load_config(None), out, workers=1)
        finally:
            realsim.GRID = old
        self.assertIn(res["verdict"]["level"], ("no", "unclear", "maybe"))
        self.assertEqual(len(res["grid"]), 2)
        for name in ("index.html", "results.json", "history.json", "summary.md"):
            self.assertTrue(os.path.exists(os.path.join(out, name)))
        html_text = open(os.path.join(out, "index.html"), encoding="utf-8").read()
        self.assertIn("儲かる？", html_text)
        # 前半で売買しないまま後半の板・指標が作られている
        self.assertGreater(res["chosen"]["second"]["hours"], 0.05)
        # 最後の切断の目印で板が消えても、取引は数えられている（以前は 0 回になっていた）
        self.assertGreater(res["default"]["trades"], 0)
        self.assertGreater(res["chosen"]["second"]["trades"] + res["chosen"]["first"]["trades"], 0)
        self.assertIsNotNone(res["default"]["avg_bps"])

    def test_no_data_page(self):
        from bfbot import realsim
        tmp = tempfile.mkdtemp()
        res = realsim.run([os.path.join(tmp, "none")], load_config(None), os.path.join(tmp, "site"), workers=1)
        self.assertIn("error", res)
        self.assertTrue(os.path.exists(os.path.join(tmp, "site", "index.html")))
