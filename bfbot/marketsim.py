"""シミュレーション用の模擬相場（FX_BTC_JPY を想定。実際の相場ではない）

数値の出どころ（2026-11-19 に bitFlyer 公式 API で見た値など）:
- スプレッド: 板の最良気配の差が約 1.6〜2.0 bps → 中央値 1.8 bps で上下させる
- 最良気配の厚み: 0.001〜0.06 BTC 程度 → 小さめの厚みで板を作る
- 約定の頻度: 約 450 件 / 19 分 → まとめた約定イベントを 0.5 回/秒
- 値動き: BTC オプションの予想変動率（年率）が約 40% → 1日あたり約 2%（既定）

「予測できるクセ」edge:
- 見えない「買いたい・売りたい圧力」L（平均ゼロ、20 秒ほど続く）を置く
- 約定の向き・板の厚みの偏りは L に連動する（bot はこれを見て判断する）
- edge = 0: L はその後の値動きと無関係（上の手がかりに予測力がない相場）
- edge > 0: L の向きに少しだけ値が動きやすい（予測力がある相場）
"""

from __future__ import annotations

import math
import random
from typing import Iterator, Tuple


def stream(days: float = 3.0, seed: int = 1, edge: float = 0.0, daily_vol: float = 0.02,
           spread_bps: float = 1.8, board_rate: float = 2.0, trade_rate: float = 0.5,
           start_price: float = 10_000_000.0, tau_latent: float = 20.0, flow_link: float = 1.2,
           book_link: float = 0.5, start_t: float = 1_763_510_400.0,
           product: str = "FX_BTC_JPY") -> Iterator[Tuple[float, str, object]]:
    """(時刻, チャンネル, メッセージ) を時刻順に返す。Realtime API と同じ形のメッセージ。"""
    rng = random.Random(seed)
    gauss, rand, expo = rng.gauss, rng.random, rng.expovariate
    ch_snap = "lightning_board_snapshot_" + product
    ch_exec = "lightning_executions_" + product
    sigma_s = daily_vol / math.sqrt(86400.0)
    t = start_t
    end = start_t + days * 86400.0
    lnm = math.log(start_price)
    L = 0.0
    total = board_rate + trade_rate
    p_board = board_rate / total
    ln_med_sp = math.log(spread_bps)

    def draw_half() -> float:
        sp = min(8.0, max(0.3, math.exp(ln_med_sp + 0.4 * gauss(0.0, 1.0))))
        return sp / 2.0 / 1e4

    half = draw_half()
    exec_id = 1
    bb = ba = 0.0
    steps = (1.0, 5.0, 20.0, 100.0, 300.0, 500.0)
    # 板の厚みと間隔は急には変わらない（段ごとに少しずつ変わる）
    fb = [1.1 * gauss(0.0, 1.0) for _ in range(10)]
    fa = [1.1 * gauss(0.0, 1.0) for _ in range(10)]
    gb = [steps[int(rand() * 6)] for _ in range(10)]
    ga = [steps[int(rand() * 6)] for _ in range(10)]
    rho = 0.95
    kk = 1.1 * math.sqrt(1.0 - rho * rho)

    def board(mid: float):
        nonlocal bb, ba
        bb = float(math.floor(mid * (1.0 - half)))
        ba = float(math.ceil(mid * (1.0 + half)))
        if ba <= bb:
            ba = bb + 1.0
        kb = math.exp(book_link * L)
        ka = math.exp(-book_link * L)
        bids, asks = [], []
        pb, pa = bb, ba
        for i in range(10):
            fb[i] = rho * fb[i] + kk * gauss(0.0, 1.0)
            fa[i] = rho * fa[i] + kk * gauss(0.0, 1.0)
            if rand() < 0.1:
                gb[i] = steps[int(rand() * 6)]
            if rand() < 0.1:
                ga[i] = steps[int(rand() * 6)]
            med = 0.01 if i == 0 else 0.03
            sb = med * math.exp(fb[i])
            sa = med * math.exp(fa[i])
            if i < 3:
                sb *= kb
                sa *= ka
            bids.append({"price": pb, "size": round(max(sb, 0.001), 8)})
            asks.append({"price": pa, "size": round(max(sa, 0.001), 8)})
            pb -= gb[i]
            pa += ga[i]
        return {"mid_price": round(mid), "bids": bids, "asks": asks}

    yield t, ch_snap, board(math.exp(lnm))
    while t < end:
        dt = expo(total)
        a = math.exp(-dt / tau_latent)
        L_new = L * a + math.sqrt(1.0 - a * a) * gauss(0.0, 1.0)  # 標準偏差 1 の OU 過程
        lnm += edge * sigma_s * 0.5 * (L + L_new) * dt + sigma_s * math.sqrt(dt) * gauss(0.0, 1.0)
        L = L_new
        t += dt
        if rand() < p_board:
            if rand() < 0.2:
                half = draw_half()
            yield t, ch_snap, board(math.exp(lnm))
        else:
            side = "BUY" if rand() < 1.0 / (1.0 + math.exp(-flow_link * L)) else "SELL"
            size = round(max(0.001, 0.01 * math.exp(1.0 * gauss(0.0, 1.0))), 8)
            yield t, ch_exec, [{"id": exec_id, "side": side, "price": ba if side == "BUY" else bb,
                                "size": size}]
            exec_id += 1
