"""傾向分析: 記録データで「各指標がその後の値動きをどれだけ当てるか」を測る

- IC（情報係数）: 指標と、h 秒後までの値動きの相関（順位相関も）
- 閾値ごとの成績: |score| が閾値を超えた時に、その向きで入って h 秒後に出たら、
  テイカーで実際に取れた値幅（買いは売値で買って h 秒後の買値で売る）
- 前半と後半に分けて同じ表を出す（片方だけ良いなら、たまたまの可能性が高い）

注意: 0.5 秒ごとの標本は h 秒の窓が重なるので、件数ほど独立ではない。
"""

from __future__ import annotations

import csv
import math
import os
from typing import Dict, List, Optional, Sequence

from .book import OrderBook
from .config import make_dc
from .features import FeatureConfig, FeatureEngine
from .util import iter_records

FEATURES = [("score", "合成スコア"), ("trend_z", "短期トレンド"), ("regime_z", "長めの傾向"),
            ("flow", "約定の偏り"), ("obi", "板の厚みの偏り"), ("ofi", "注文の流れOFI")]
COLS = ("t", "bid", "ask", "mid", "spread_bps", "vol") + tuple(k for k, _ in FEATURES)


def collect_samples(paths, cfg: dict, sample_sec: float = 0.5) -> Dict[str, list]:
    pc = cfg["product_code"]
    ch_snap, ch_board, ch_exec = (f"lightning_board_snapshot_{pc}", f"lightning_board_{pc}",
                                  f"lightning_executions_{pc}")
    book = OrderBook()
    fe = FeatureEngine(make_dc(FeatureConfig, cfg["features"]))
    out: Dict[str, list] = {k: [] for k in COLS}
    next_t: Optional[float] = None
    for rec in iter_records(paths):
        ch, t, m = rec.get("ch"), rec.get("t"), rec.get("m")
        if t is None:
            continue
        if ch == ch_board:
            book.apply_diff(m, t)
            fe.on_book(book, t)
        elif ch == ch_snap:
            book.apply_snapshot(m, t)
            fe.on_book(book, t)
        elif ch == ch_exec:
            for e in m or ():
                try:
                    fe.on_execution(e.get("side") or "", float(e["size"]), float(e["price"]), t)
                except (AttributeError, KeyError, TypeError, ValueError):
                    continue
        elif ch == "_disconnect":
            book.reset()
            fe.reset_flow()
            next_t = None
            continue
        else:
            continue
        if not book.ready:
            continue
        if next_t is None or t >= next_t:
            next_t = t + sample_sec
            f = fe.compute(book, t)
            if not f["warm"]:
                continue
            out["t"].append(t)
            out["bid"].append(f["bid"])
            out["ask"].append(f["ask"])
            out["mid"].append(f["mid"])
            out["spread_bps"].append(f["spread_bps"])
            out["vol"].append(f["vol_bps_1m"])
            for k, _ in FEATURES:
                out[k].append(f[k])
    return out


def forward_index(ts: Sequence[float], h: float, max_gap: float) -> List[int]:
    """各時刻から h 秒後の標本の位置（途中に切断などの空白があれば -1）"""
    n = len(ts)
    out = [-1] * n
    j = 0
    for i in range(n):
        target = ts[i] + h
        if j < i:
            j = i
        while j < n and ts[j] < target:
            j += 1
        if j < n and ts[j] - target <= max_gap:
            out[i] = j
    return out


def pearson(x: Sequence[float], y: Sequence[float]) -> Optional[float]:
    n = len(x)
    if n < 3:
        return None
    mx, my = sum(x) / n, sum(y) / n
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx <= 0 or syy <= 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / math.sqrt(sxx * syy)


def ranks(x: Sequence[float]) -> List[float]:
    order = sorted(range(len(x)), key=lambda i: x[i])
    r = [0.0] * len(x)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and x[order[j + 1]] == x[order[i]]:
            j += 1
        avg = (i + j) / 2.0
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r


def spearman(x, y) -> Optional[float]:
    return pearson(ranks(x), ranks(y))


def _threshold_table(c, rows: Sequence[int], idx: List[int], thresholds) -> List[dict]:
    out = []
    for th in thresholds:
        n = hit = 0
        mv = tk = 0.0
        for i in rows:
            j = idx[i]
            s = c["score"][i]
            if j < 0 or abs(s) < th:
                continue
            if s > 0:
                move = (c["mid"][j] / c["mid"][i] - 1) * 1e4
                taker = (c["bid"][j] - c["ask"][i]) / c["ask"][i] * 1e4
            else:
                move = (1 - c["mid"][j] / c["mid"][i]) * 1e4
                taker = (c["bid"][i] - c["ask"][j]) / c["bid"][i] * 1e4
            n += 1
            mv += move
            tk += taker
            if move > 0:
                hit += 1
        out.append({"閾値": th, "件数": n, "的中率": hit / n if n else None,
                    "平均の値動き_bps": mv / n if n else None, "テイカー損益_bps": tk / n if n else None})
    return out


def analyze(paths, cfg: dict, horizons=(1, 5, 10, 30, 60), sample_sec: float = 0.5,
            thresholds=(0.2, 0.3, 0.4, 0.5, 0.6, 0.7), out_csv: Optional[str] = None,
            verbose: bool = True) -> dict:
    c = collect_samples(paths, cfg, sample_sec)
    n = len(c["t"])
    p = print if verbose else (lambda *a, **k: None)
    if n < 200:
        p(f"標本が {n} 件しかありません。もっと長く記録してください（目安: 数時間以上）。")
        return {"samples": n}
    hours = (c["t"][-1] - c["t"][0]) / 3600.0
    sp = sorted(c["spread_bps"])
    vols = sorted(v for v in c["vol"] if v is not None)
    p("\n========== 傾向分析 ==========")
    p(f"標本 {n:,} 件（{sample_sec}秒ごと）/ 期間 {hours:.2f} 時間")
    p(f"スプレッド 中央値 {sp[len(sp) // 2]:.2f} bps（90% {sp[int(len(sp) * 0.9)]:.2f} bps）")
    if vols:
        p(f"1分あたりの値動き（標準偏差）中央値 {vols[len(vols) // 2]:.1f} bps")
    result = {"samples": n, "hours": hours, "spread_median_bps": sp[len(sp) // 2], "by_h": {}}
    csv_rows = []
    half = n // 2
    for h in horizons:
        idx = forward_index(c["t"], h, max_gap=max(5.0, float(h)))
        valid = [i for i in range(n) if idx[i] >= 0]
        if len(valid) < 100:
            continue
        r = [(c["mid"][idx[i]] / c["mid"][i] - 1) * 1e4 for i in valid]
        absr = sorted(abs(x) for x in r)
        p(f"\n--- {h} 秒後 ---  典型的な値動き |Δ| 中央値 {absr[len(absr) // 2]:.2f} bps"
          f" / 平均 {sum(absr) / len(absr):.2f} bps")
        ics = {}
        line = []
        for k, name in FEATURES:
            x = [c[k][i] for i in valid]
            ic, ric = pearson(x, r), spearman(x, r)
            ics[k] = (ic, ric)
            line.append(f"{name} {ic if ic is None else round(ic, 3)}/{ric if ric is None else round(ric, 3)}")
        p("IC（相関/順位相関）: " + " | ".join(line))
        tables = {}
        for label, rows in (("全体", valid), ("前半", [i for i in valid if i < half]),
                            ("後半", [i for i in valid if i >= half])):
            tables[label] = _threshold_table(c, rows, idx, thresholds)
        p(f"{'閾値':>4} | {'全体: 件数 的中 値動き テイカー':^30} | {'前半 テイカー':^10} | {'後半 テイカー':^10}")
        for k2, row in enumerate(tables["全体"]):
            def fmt(v, d=2):
                return "  -  " if v is None else f"{v:+.{d}f}"
            a, b = tables["前半"][k2], tables["後半"][k2]
            hit = "  -  " if row["的中率"] is None else f"{row['的中率'] * 100:4.1f}%"
            p(f"{row['閾値']:>4} | {row['件数']:6d} {hit} {fmt(row['平均の値動き_bps'])} "
              f"{fmt(row['テイカー損益_bps'])} | {fmt(a['テイカー損益_bps']):>10} | {fmt(b['テイカー損益_bps']):>10}")
            csv_rows.append({"h秒": h, **row, "前半テイカー_bps": a["テイカー損益_bps"],
                             "後半テイカー_bps": b["テイカー損益_bps"]})
        result["by_h"][h] = {"ic": ics, "typical_abs_bps": absr[len(absr) // 2], "table": tables}
    good = [r for r in csv_rows if r["件数"] >= 50 and (r["テイカー損益_bps"] or 0) > 0
            and (r["前半テイカー_bps"] or 0) > 0 and (r["後半テイカー_bps"] or 0) > 0]
    p("\n--- まとめ ---")
    if good:
        p("前半・後半ともテイカー損益がプラスの組み合わせ（ここから設定を詰める候補）:")
        for r in good:
            p(f"  {r['h秒']}秒・閾値 {r['閾値']}: {r['テイカー損益_bps']:+.2f} bps × {r['件数']} 件")
    else:
        p("コストを引いて前半・後半ともプラスになる組み合わせはありませんでした。"
          "この設定のまま本番に出すのはやめてください。")
    p("※ 標本は窓が重なるので件数ほど独立ではありません。過去で良くても将来も良いとは限りません。")
    result["good"] = good
    if out_csv:
        os.makedirs(os.path.dirname(out_csv) or ".", exist_ok=True)
        with open(out_csv, "w", newline="", encoding="utf-8") as fh:
            if csv_rows:
                w = csv.DictWriter(fh, fieldnames=list(csv_rows[0].keys()))
                w.writeheader()
                w.writerows(csv_rows)
        p(f"表を保存: {out_csv}")
    return result
