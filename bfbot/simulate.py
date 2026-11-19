"""「儲かるか」のシミュレーション

模擬相場（marketsim）のメッセージを、bot 本体（Engine＋PaperBroker）にそのまま流して売買させる。
相場の「予測できるクセ」の強さを変えて、同じ bot がどうなるかを比べる。
実際の相場にどれくらいクセがあるかは、記録した実データで `analyze` を使って測ること。
"""

from __future__ import annotations

import copy
import logging
import multiprocessing as mp
import statistics
import time
from typing import List, Optional

from .analyze import forward_index, pearson
from .config import deep_merge, load_config
from .engine import Engine, make_paper_broker
from .marketsim import stream

SCENARIOS = [("なし", 0.0), ("弱い", 0.03), ("中くらい", 0.06), ("強い", 0.12)]

_BIG_RISK = {"max_position_btc": 0.02, "max_daily_loss_jpy": 30000.0, "max_drawdown_jpy": 50000.0}
VARIANTS = [
    ("既定 0.001BTC", {}),
    ("既定 0.01BTC", {"strategy": {"size": 0.01}, "risk": dict(_BIG_RISK)}),
    ("長め保有 0.01BTC", {"strategy": {"size": 0.01, "entry_threshold": 0.55, "exit_on_reverse": 2.0,
                                     "take_profit_bps": 12.0, "stop_loss_bps": 8.0, "max_hold_sec": 120.0,
                                     "cooldown_sec": 5.0}, "risk": dict(_BIG_RISK)}),
]


def run_one(task) -> dict:
    name, edge, seed, days, overrides, market_kw = task
    logging.getLogger("bfbot").setLevel(logging.ERROR)  # 連敗休止などの表示を抑える
    t0 = time.time()
    engines = []
    for vname, ov in VARIANTS:
        cfg = load_config(None, copy.deepcopy(overrides or {}))
        deep_merge(cfg, copy.deepcopy(ov))
        engines.append((vname, Engine(cfg, make_paper_broker(cfg), mode="backtest", quiet=True)))
    ts: List[float] = []
    sc: List[float] = []
    mids: List[float] = []
    next_s: Optional[float] = None
    next_day: Optional[float] = None
    day_eq: List[list] = []
    n = 0
    for t, ch, m in stream(days=days, seed=seed, edge=edge, **(market_kw or {})):
        if next_day is None:
            next_day = t + 86400.0
        for _, e in engines:
            e.on_message(ch, m, t)
        n += 1
        if next_s is None or t >= next_s:
            f = engines[0][1].last_features
            if f is not None and f.get("warm"):
                ts.append(t)
                sc.append(f["score"])
                mids.append(f["mid"])
            next_s = t + 1.0
        if t >= next_day:
            day_eq.append([e.last_equity for _, e in engines])
            next_day += 86400.0
    variants = []
    for vname, e in engines:
        if e.book.ready and e.last_t is not None:
            e.broker.force_close(e.book, e.last_t, reason="終了")
            e.last_equity = e.broker.equity(e.book.best_bid, e.book.best_ask)
        s = e.summary()
        variants.append({"variant": vname, "trips": s["往復数"], "win": s["勝率"], "pnl": s["損益合計_円"],
                         "end": s["終了資産_円"], "mult": s["倍率"], "mdd": s["最大ドローダウン_円"],
                         "halt": s["停止理由"], "avg_bps": s["1往復平均_bps"], "orders": s["注文数"],
                         "exits": s["決済理由"]})
    idx = forward_index(ts, 30.0, max_gap=5.0)
    xs = [sc[i] for i in range(len(ts)) if idx[i] >= 0]
    ys = [(mids[idx[i]] / mids[i] - 1.0) * 1e4 for i in range(len(ts)) if idx[i] >= 0]
    return {"scenario": name, "edge": edge, "seed": seed, "days": days, "msgs": n,
            "ic30": pearson(xs, ys), "variants": variants, "day_eq": day_eq, "sec": time.time() - t0}


def run_all(days: float = 3.0, seeds: int = 3, workers: int = 2, scenarios=None, overrides=None,
            market_kw=None, progress=print) -> List[dict]:
    scenarios = scenarios or SCENARIOS
    tasks = [(name, edge, seed, days, overrides, market_kw)
             for name, edge in scenarios for seed in range(1, seeds + 1)]
    out = []
    if workers <= 1:
        for tk in tasks:
            r = run_one(tk)
            out.append(r)
            progress(f"  済 {r['scenario']} seed{r['seed']}（{r['sec']:.0f}秒）")
        return out
    with mp.Pool(workers) as pool:
        for r in pool.imap_unordered(run_one, tasks):
            out.append(r)
            progress(f"  済 {r['scenario']} seed{r['seed']}（{r['sec']:.0f}秒）")
    return out


def report(results: List[dict], capital: float = 100000.0, p=print) -> List[dict]:
    rows = []
    names = []
    for r in results:
        if r["scenario"] not in names:
            names.append(r["scenario"])
    p("\n予測できるクセ | 予測力IC | 設定 | 往復/日 | 勝率 | 3日の損益（中央値・最小〜最大） | 黒字 | 停止")
    for name in names:
        rs = [r for r in results if r["scenario"] == name]
        ics = [r["ic30"] for r in rs if r["ic30"] is not None]
        ic = statistics.mean(ics) if ics else float("nan")
        for vi in range(len(rs[0]["variants"])):
            vs = [r["variants"][vi] for r in rs]
            days = rs[0]["days"]
            pnl = sorted(v["end"] - capital for v in vs)
            trips = statistics.median(v["trips"] for v in vs) / days
            wins = [v["win"] for v in vs if v["win"] is not None]
            win = statistics.median(wins) if wins else None
            prof = sum(1 for x in pnl if x > 0)
            halts = sum(1 for v in vs if v["halt"])
            row = {"scenario": name, "ic": ic, "variant": vs[0]["variant"], "trips_per_day": trips,
                   "win": win, "pnl_med": statistics.median(pnl), "pnl_min": pnl[0], "pnl_max": pnl[-1],
                   "profitable": prof, "n": len(pnl), "halts": halts,
                   "mult_med": statistics.median(v["end"] / capital for v in vs)}
            rows.append(row)
            ws = "-" if win is None else f"{win * 100:.1f}%"
            p(f"{name:>6} | {ic:+.3f} | {row['variant']} | {trips:6.0f} | {ws} | "
              f"{row['pnl_med']:+,.0f}円（{row['pnl_min']:+,.0f}〜{row['pnl_max']:+,.0f}） | "
              f"{prof}/{len(pnl)} | {halts}/{len(pnl)}")
    return rows
