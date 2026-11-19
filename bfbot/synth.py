"""動作確認用の合成データ（実際の相場ではない）

約定の偏りが少し先の値動きを動かす、作り物の市場を生成する。
プログラムが最後まで動くかを確かめるためのもので、ここでの損益に意味はない。
"""

from __future__ import annotations

import gzip
import json
import math
import random

from .util import iso_utc


def generate(path: str, minutes: float = 60.0, seed: int = 7, product: str = "FX_BTC_JPY",
             start_price: float = 15_000_000.0, daily_vol: float = 0.025, msg_rate: float = 20.0,
             start_t: float = 1_763_510_400.0, edge: float = 3e-5) -> int:
    rng = random.Random(seed)
    ch_snap = "lightning_board_snapshot_" + product
    ch_board = "lightning_board_" + product
    ch_exec = "lightning_executions_" + product
    sigma_s = daily_vol / math.sqrt(86400.0)
    t = start_t
    end = start_t + minutes * 60.0
    mid = start_price
    latent = 0.0
    prev_b: dict = {}
    prev_a: dict = {}
    next_snap = t
    exec_id = 1
    count = 0
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        def w(ch, m):
            nonlocal count
            fh.write(json.dumps({"t": round(t, 6), "ch": ch, "m": m}, separators=(",", ":")) + "\n")
            count += 1

        while t < end:
            dt = rng.expovariate(msg_rate)
            t += dt
            latent = latent * math.exp(-dt / 20.0) + rng.gauss(0.0, 0.2 * math.sqrt(dt))
            mid *= math.exp(edge * latent * dt + sigma_s * math.sqrt(dt) * rng.gauss(0.0, 1.0))
            half = rng.choice([150, 250, 400, 600, 900])
            bb, ba = math.floor(mid - half), math.ceil(mid + half)
            imb = math.tanh(latent)
            bids, asks = {}, {}
            pb, pa = bb, ba
            for i in range(15):
                kb = 1.0 + 0.8 * imb if i < 3 else 1.0
                ka = 1.0 - 0.8 * imb if i < 3 else 1.0
                bids[float(pb)] = round(rng.uniform(0.01, 0.3) * kb, 8)
                asks[float(pa)] = round(rng.uniform(0.01, 0.3) * ka, 8)
                pb -= rng.choice([1, 50, 100, 200, 300])
                pa += rng.choice([1, 50, 100, 200, 300])
            if t >= next_snap:
                w(ch_snap, {"mid_price": round(mid),
                            "bids": [{"price": p, "size": s} for p, s in bids.items()],
                            "asks": [{"price": p, "size": s} for p, s in asks.items()]})
                next_snap = t + 10.0
            else:
                db = [{"price": p, "size": s} for p, s in bids.items() if prev_b.get(p) != s]
                db += [{"price": p, "size": 0} for p in prev_b if p not in bids]
                da = [{"price": p, "size": s} for p, s in asks.items() if prev_a.get(p) != s]
                da += [{"price": p, "size": 0} for p in prev_a if p not in asks]
                w(ch_board, {"mid_price": round(mid), "bids": db, "asks": da})
            prev_b, prev_a = bids, asks
            if rng.random() < min(1.0, 6.0 * dt):
                pbuy = 1.0 / (1.0 + math.exp(-2.5 * latent))
                side = "BUY" if rng.random() < pbuy else "SELL"
                price = float(ba if side == "BUY" else bb)
                size = round(max(0.001, rng.lognormvariate(math.log(0.01), 1.0)), 8)
                w(ch_exec, [{"id": exec_id, "side": side, "price": price, "size": size,
                             "exec_date": iso_utc(t - 0.03),
                             "buy_child_order_acceptance_id": "JRF-SYNTH",
                             "sell_child_order_acceptance_id": "JRF-SYNTH"}])
                exec_id += 1
    return count
