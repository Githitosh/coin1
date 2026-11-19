"""実データで「儲かるか」を調べる（記録した bitFlyer 公式 Realtime API のデータを使う）

1. 傾向分析: 各指標がその後の値動きを当てるか（スプレッドを払った後で）
2. 設定の候補を記録の「前半」だけで比べて1つ選び、選ぶのに使っていない「後半」で成績を見る
   （同じデータで選んで同じデータで測ると、良く見えすぎるため）
3. その成績で3日続けた場合を計算する（100倍に届くか）
4. 結果を index.html（スマホ向けの1枚のページ）と results.json に書き出す
"""

from __future__ import annotations

import copy
import html
import json
import logging
import math
import os
import statistics
import time
from concurrent.futures import ProcessPoolExecutor
from typing import List, Optional

from .analyze import analyze
from .engine import run_backtest
from .target import hold_price_multiple, prob_reach, required_rates
from .util import iter_records, jst_str

GRID = [{"entry_threshold": th, "order_mode": mode, "max_hold_sec": hold}
        for th in (0.3, 0.45, 0.6) for mode in ("taker", "maker") for hold in (15.0, 60.0)]


def scan(paths):
    t0 = t1 = None
    n = 0
    for r in iter_records(paths):
        t = r.get("t")
        if t is None or r.get("ch") == "_disconnect":
            continue
        if t0 is None:
            t0 = t
        t1 = t
        n += 1
    return t0, t1, n


def _downsample(curve, n=240):
    if len(curve) <= n:
        return curve
    step = len(curve) / n
    return [curve[int(i * step)] for i in range(n)] + [curve[-1]]


def trade_stats(eng, cfg) -> dict:
    s = eng.summary()
    size = float(cfg["strategy"]["size"])
    mid = eng.book.mid or 0.0
    notional = size * mid
    pnls = [p for _, p, _ in eng.broker.pos.round_trips]
    bps = [p / notional * 1e4 for p in pnls] if notional > 0 else []
    wins = [b for b in bps if b > 0]
    losses = [-b for b in bps if b <= 0]
    n = len(bps)
    mean = sum(bps) / n if n else None
    sd = statistics.pstdev(bps) if n > 1 else 0.0
    hours = s["期間_時間"] or 0.0
    start = s["開始資産_円"]
    return {
        "trades": n,
        "win_rate": len(wins) / n if n else None,
        "pnl_jpy": sum(pnls),
        "avg_bps": mean,
        "avg_win_bps": sum(wins) / len(wins) if wins else None,
        "avg_loss_bps": sum(losses) / len(losses) if losses else None,
        "t_stat": (mean / (sd / math.sqrt(n))) if (n > 1 and sd > 0) else None,
        "max_dd_jpy": s["最大ドローダウン_円"],
        "orders": s["注文数"],
        "hours": hours,
        "trades_per_hour": n / hours if hours > 0 else None,
        "exits": s["決済理由"],
        "halt": s["停止理由"],
        "mid": mid,
        "equity": [(round(t, 1), round(e - start, 2)) for t, e in _downsample(eng.equity_curve)],
    }


def _job(args):
    key, cfg, paths, t_from, t_to = args
    logging.getLogger("bfbot").setLevel(logging.ERROR)
    eng = run_backtest(cfg, paths, t_from=t_from, t_to=t_to)
    return key, trade_stats(eng, cfg)


def _with(cfg: dict, params: dict) -> dict:
    c = copy.deepcopy(cfg)
    c["strategy"].update(params)
    return c


def _verdict(oos: dict) -> dict:
    n = oos["trades"]
    avg = oos["avg_bps"]
    if n < 10:
        return {"level": "unclear", "label": "まだ判断できない",
                "detail": f"選ぶのに使っていない後半の取引が {n} 回しかありません。もっと長く記録して確かめます。"}
    if oos["pnl_jpy"] <= 0:
        return {"level": "no", "label": "儲からない",
                "detail": (f"記録の前半で一番良かった設定でも、後半（未知のデータ）では1回あたり平均 {avg:+.2f} bps"
                           f"（{n} 回）でした。スプレッドを払うとマイナスです。")}
    ts = oos["t_stat"]
    if ts is None or ts < 2.0:
        return {"level": "unclear", "label": "プラスだが偶然と区別できない",
                "detail": (f"後半で1回あたり平均 {avg:+.2f} bps（{n} 回）とプラスですが、ばらつきに対して小さく"
                           f"（t値 {0 if ts is None else ts:.1f}、2未満）、たまたまの可能性が高いです。")}
    return {"level": "maybe", "label": "この記録ではプラス",
            "detail": (f"後半で1回あたり平均 {avg:+.2f} bps（{n} 回、t値 {ts:.1f}）。記録が短いので、"
                       f"別の日にも記録して同じ結果になるか確かめてください。")}


def run(paths, cfg: dict, out_dir: str, run_url: str = "", workers: Optional[int] = None,
        model_path: Optional[str] = None) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    t0, t1, n = scan(paths)
    res: dict = {"generated": time.time(), "generated_jst": jst_str()[:16], "product": cfg["product_code"],
                 "run_url": run_url, "capital": float(cfg["capital_jpy"])}
    if not n or t1 - t0 < 600:
        res["error"] = "データがほとんど取れませんでした（bitFlyer に接続できなかった可能性があります）"
        res["data"] = {"records": n}
        _write(res, out_dir, model_path)
        return res
    hours = (t1 - t0) / 3600.0
    res["data"] = {"records": n, "t0": t0, "t1": t1, "hours": hours, "from_jst": jst_str(t0)[:16],
                   "to_jst": jst_str(t1)[:16],
                   "source": "bitFlyer 公式 Realtime API（wss://ws.lightstream.bitflyer.com/json-rpc）"}

    a = analyze(paths, cfg, verbose=False)
    res["analysis"] = {
        "spread_median_bps": a.get("spread_median_bps"),
        "by_h": {str(h): {"ic": {k: v[0] for k, v in d["ic"].items()},
                          "typical_abs_bps": d["typical_abs_bps"],
                          "table": d["table"]["全体"]} for h, d in a.get("by_h", {}).items()},
        "good": a.get("good", []),
    }

    t_mid = t0 + (t1 - t0) / 2.0
    jobs = [("default", cfg, paths, None, None)]
    for i, g in enumerate(GRID):
        c = _with(cfg, g)
        jobs.append((("first", i), c, paths, None, t_mid))
        jobs.append((("second", i), c, paths, t_mid, None))
    workers = workers or max(1, min(len(jobs), os.cpu_count() or 1))
    out = {}
    if workers == 1:
        for j in jobs:
            k, st = _job(j)
            out[k] = st
    else:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            for k, st in ex.map(_job, jobs):
                out[k] = st
    grid = [{"params": g, "first": out[("first", i)], "second": out[("second", i)]} for i, g in enumerate(GRID)]
    for g in grid:  # 表には曲線を載せない（重くなるため）
        g["first"] = {k: v for k, v in g["first"].items() if k != "equity"}
    eligible = [g for g in grid if g["first"]["trades"] >= 5] or grid
    chosen = max(eligible, key=lambda g: g["first"]["pnl_jpy"])
    ci = grid.index(chosen)
    chosen = {"params": chosen["params"], "first": chosen["first"], "second": out[("second", ci)]}
    for g in grid:
        g["second"] = {k: v for k, v in g["second"].items() if k != "equity"}
    res["grid"] = grid
    res["chosen"] = chosen
    res["default"] = out["default"]
    oos = chosen["second"]
    res["verdict"] = _verdict(oos)

    size = float(cfg["strategy"]["size"])
    mid = oos["mid"] or out["default"]["mid"]
    k = (2.0 * res["capital"] / (size * mid)) if mid else 0.0
    res["scale"] = {"factor": k, "pnl_jpy": oos["pnl_jpy"] * k,
                    "per_day_jpy": oos["pnl_jpy"] * k * 24.0 / oos["hours"] if oos["hours"] else None}
    res["required"] = required_rates(100.0, 3.0)
    res["hold_price_multiple"] = hold_price_multiple(100.0, 2.0)
    if oos["avg_win_bps"] and oos["avg_loss_bps"] and oos["trades_per_hour"]:
        res["projection"] = prob_reach(oos["win_rate"], oos["avg_win_bps"], oos["avg_loss_bps"],
                                       oos["trades_per_hour"] * 24.0, 3.0, 2.0, 100.0)
    _write(res, out_dir, model_path)
    return res


# ------------------------------------------------------------------ 書き出し
def _write(res: dict, out_dir: str, model_path: Optional[str]) -> None:
    hist_path = os.path.join(out_dir, "history.json")
    history = []
    if os.path.exists(hist_path):
        try:
            with open(hist_path, encoding="utf-8") as fh:
                history = json.load(fh)
        except (OSError, json.JSONDecodeError):
            history = []
    if "error" not in res:
        oos = res["chosen"]["second"]
        history.insert(0, {"jst": res["generated_jst"], "hours": res["data"]["hours"], "trades": oos["trades"],
                           "avg_bps": oos["avg_bps"], "win_rate": oos["win_rate"], "label": res["verdict"]["label"],
                           "level": res["verdict"]["level"], "spread_bps": res["analysis"]["spread_median_bps"],
                           "run_url": res.get("run_url", "")})
        history = history[:50]
    with open(hist_path, "w", encoding="utf-8") as fh:
        json.dump(history, fh, ensure_ascii=False, indent=1)
    model = None
    if model_path and os.path.exists(model_path):
        try:
            with open(model_path, encoding="utf-8") as fh:
                model = json.load(fh)
        except (OSError, json.JSONDecodeError):
            model = None
    with open(os.path.join(out_dir, "results.json"), "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False)
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(build_html(res, history, model))
    with open(os.path.join(out_dir, "summary.md"), "w", encoding="utf-8") as fh:
        fh.write(summary_md(res))


def summary_md(res: dict) -> str:
    if "error" in res:
        return f"## シミュレーション\n\n{res['error']}\n"
    v, o = res["verdict"], res["chosen"]["second"]
    return (f"## 儲かる？ → {v['label']}\n\n{v['detail']}\n\n"
            f"- 記録: {res['data']['from_jst']} 〜 {res['data']['to_jst']}（{res['data']['hours']:.2f} 時間）\n"
            f"- 後半の成績: {o['trades']} 回 / 勝率 {_pct(o['win_rate'])} / 1回平均 {_f(o['avg_bps'])} bps\n"
            f"- スプレッド中央値: {_f(res['analysis']['spread_median_bps'], sign=False)} bps\n")


def _f(x, d=2, sign=True) -> str:
    if x is None:
        return "-"
    return f"{x:+.{d}f}" if sign else f"{x:.{d}f}"


def _pct(x) -> str:
    return "-" if x is None else f"{x * 100:.1f}%"


def _yen(x) -> str:
    return "-" if x is None else f"{x:+,.0f} 円"


def _svg(curve, w=640, h=200) -> str:
    if not curve or len(curve) < 2:
        return ""
    xs = [c[0] for c in curve]
    ys = [c[1] for c in curve]
    x0, x1 = xs[0], xs[-1]
    lo, hi = min(min(ys), 0.0), max(max(ys), 0.0)
    if hi - lo < 1e-9:
        hi = lo + 1.0
    pad = 10

    def X(x):
        return pad + (x - x0) / ((x1 - x0) or 1) * (w - 2 * pad)

    def Y(y):
        return pad + (hi - y) / (hi - lo) * (h - 2 * pad)

    pts = " ".join(f"{X(x):.1f},{Y(y):.1f}" for x, y in curve)
    color = "#15803d" if ys[-1] > 0 else "#b91c1c"
    return (f'<svg viewBox="0 0 {w} {h}" class="chart" role="img" aria-label="損益の推移">'
            f'<line x1="{pad}" x2="{w - pad}" y1="{Y(0):.1f}" y2="{Y(0):.1f}" class="zero"/>'
            f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2"/>'
            f'<text x="{w - pad}" y="{h - 2}" text-anchor="end" class="ax">{html.escape(_yen(ys[-1]))}</text>'
            f'<text x="{pad}" y="{Y(0) - 4:.1f}" class="ax">0</text></svg>')


CSS = """
:root{--fg:#111827;--mute:#6b7280;--line:#e5e7eb;--card:#f9fafb;--no:#b91c1c;--no-bg:#fef2f2;
--un:#92400e;--un-bg:#fffbeb;--ok:#15803d;--ok-bg:#f0fdf4}
*{box-sizing:border-box}body{margin:0;background:#fff;color:var(--fg);
font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans","Noto Sans JP",sans-serif;line-height:1.6}
main{max-width:760px;margin:0 auto;padding:20px 16px 48px}
.meta{color:var(--mute);font-size:13px;margin:0 0 4px}h1{font-size:22px;margin:0 0 4px}
.sub{color:var(--mute);margin:0 0 20px;font-size:14px}h2{font-size:17px;margin:32px 0 10px}
.verdict{border-radius:14px;padding:18px;margin:8px 0 8px}
.verdict .q{font-size:14px;font-weight:600}.verdict .a{font-size:30px;font-weight:800;margin:2px 0 6px}
.verdict p{margin:0}.no{background:var(--no-bg);color:var(--no)}.unclear{background:var(--un-bg);color:var(--un)}
.maybe{background:var(--ok-bg);color:var(--ok)}
.cards{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.card .k{font-size:12px;color:var(--mute)}.card .v{font-size:20px;font-weight:700}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:6px 4px;border-bottom:1px solid var(--line);
text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left}th{color:var(--mute);font-weight:600}
.scroll{overflow-x:auto}.chart{width:100%;height:auto;background:var(--card);border:1px solid var(--line);
border-radius:10px;margin-top:8px}.zero{stroke:#9ca3af;stroke-dasharray:4 4}.ax{font-size:12px;fill:#6b7280}
.neg{color:var(--no)}.pos{color:var(--ok)}.note{color:var(--mute);font-size:13px}
a{color:#1d4ed8}footer{margin-top:36px;color:var(--mute);font-size:12px}
"""


def _cls(x) -> str:
    if x is None:
        return ""
    return "pos" if x > 0 else ("neg" if x < 0 else "")


def build_html(res: dict, history: List[dict], model: Optional[dict]) -> str:
    e = html.escape
    parts = [f"<p class='meta'>作成日時：{e(res['generated_jst'])}（日本時間）</p>",
             "<h1>coin1 — bitFlyer bot のシミュレーション</h1>",
             "<p class='sub'>bitFlyer の公式 Realtime API から GitHub 上で記録した実データで、"
             "bot が儲かるかを調べた結果です。お金は動かしていません（模擬売買）。</p>"]
    if "error" in res:
        parts.append(f"<section class='verdict unclear'><div class='q'>儲かる？</div>"
                     f"<div class='a'>記録できませんでした</div><p>{e(res['error'])}</p></section>")
    else:
        v, d, o, ch = res["verdict"], res["data"], res["chosen"]["second"], res["chosen"]
        parts.append(f"<section class='verdict {e(v['level'])}'><div class='q'>儲かる？</div>"
                     f"<div class='a'>{e(v['label'])}</div><p>{e(v['detail'])}</p></section>")
        parts.append("<h2>記録したデータ</h2><div class='cards'>"
                     f"<div class='card'><div class='k'>期間</div><div class='v'>{d['hours']:.1f} 時間</div>"
                     f"<div class='k'>{e(d['from_jst'])} 〜 {e(d['to_jst'][11:])}</div></div>"
                     f"<div class='card'><div class='k'>受信件数</div><div class='v'>{d['records']:,}</div></div>"
                     f"<div class='card'><div class='k'>スプレッド（中央値）</div>"
                     f"<div class='v'>{_f(res['analysis']['spread_median_bps'], sign=False)} bps</div>"
                     "<div class='k'>テイカーは1往復でこの分を払う</div></div>")
        typ = res["analysis"]["by_h"]
        if "30" in typ:
            parts.append(f"<div class='card'><div class='k'>30秒の値動き（中央値）</div>"
                         f"<div class='v'>{typ['30']['typical_abs_bps']:.2f} bps</div></div>")
        parts.append("</div>")
        p = ch["params"]
        parts.append("<h2>未知のデータでの成績（記録の後半）</h2>"
                     f"<p class='note'>前半だけで比べて選んだ設定（入る強さ {p['entry_threshold']}・"
                     f"{'テイカー' if p['order_mode'] == 'taker' else 'メイカー'}・最長 {p['max_hold_sec']:.0f} 秒）を、"
                     "選ぶのに使っていない後半で動かした結果です。1回 0.001 BTC。</p><div class='cards'>"
                     f"<div class='card'><div class='k'>取引（往復）</div><div class='v'>{o['trades']} 回</div></div>"
                     f"<div class='card'><div class='k'>勝率</div><div class='v'>{_pct(o['win_rate'])}</div></div>"
                     f"<div class='card'><div class='k'>1回あたり平均（コスト後）</div>"
                     f"<div class='v {_cls(o['avg_bps'])}'>{_f(o['avg_bps'])} bps</div></div>"
                     f"<div class='card'><div class='k'>損益（0.001 BTC）</div>"
                     f"<div class='v {_cls(o['pnl_jpy'])}'>{_yen(o['pnl_jpy'])}</div></div>"
                     f"<div class='card'><div class='k'>10万円・2倍で回した換算</div>"
                     f"<div class='v {_cls(res['scale']['pnl_jpy'])}'>{_yen(res['scale']['pnl_jpy'])}</div>"
                     f"<div class='k'>1日に直すと {_yen(res['scale']['per_day_jpy'])}</div></div>"
                     f"<div class='card'><div class='k'>最大の落ち込み</div><div class='v'>{o['max_dd_jpy']:,.0f} 円</div></div>"
                     "</div>" + _svg(o["equity"]))
        rows = []
        for g in res["grid"]:
            gp, f1, f2 = g["params"], g["first"], g["second"]
            mark = " ←" if gp == p else ""
            rows.append(f"<tr><td>{gp['entry_threshold']} / {'T' if gp['order_mode'] == 'taker' else 'M'} / "
                        f"{gp['max_hold_sec']:.0f}秒{mark}</td>"
                        f"<td class='{_cls(f1['avg_bps'])}'>{_f(f1['avg_bps'])}</td><td>{f1['trades']}</td>"
                        f"<td class='{_cls(f2['avg_bps'])}'>{_f(f2['avg_bps'])}</td><td>{f2['trades']}</td></tr>")
        parts.append("<h2>設定の比較</h2><p class='note'>入る強さ / T=テイカー・M=メイカー / 最長保有。"
                     "数字は1回あたり平均（bps、コスト後）と回数。← が前半で選んだ設定。</p>"
                     "<div class='scroll'><table><tr><th>設定</th><th>前半</th><th>回</th><th>後半</th><th>回</th></tr>"
                     + "".join(rows) + "</table></div>")
        names = [("score", "合成"), ("trend_z", "トレンド"), ("flow", "約定の偏り"), ("obi", "板の偏り"),
                 ("ofi", "注文の流れ")]
        rows = []
        for h, dd in typ.items():
            best = max((r for r in dd["table"] if r["件数"] >= 30), key=lambda r: r["テイカー損益_bps"] or -1e9,
                       default=None)
            cells = "".join(f"<td>{_f(dd['ic'].get(k), 3)}</td>" for k, _ in names)
            b = "-" if best is None else f"{best['テイカー損益_bps']:+.2f}"
            rows.append(f"<tr><td>{e(h)}秒後</td>{cells}<td class='{_cls(None if best is None else best['テイカー損益_bps'])}'>{b}</td></tr>")
        parts.append("<h2>傾向分析（指標は値動きを当てるか）</h2><p class='note'>数字は指標と、その時間後までの値動きの相関（0 なら無関係）。"
                     "右端は一番良い閾値でテイカー売買した時の1回あたり損益（bps、スプレッド込み）。</p>"
                     "<div class='scroll'><table><tr><th>先</th>" + "".join(f"<th>{e(n)}</th>" for _, n in names)
                     + "<th>取れた値幅</th></tr>" + "".join(rows) + "</table></div>")
        rr = res["required"]
        parts.append("<h2>3日で100倍（10万円→1000万円）は？</h2><ul>"
                     f"<li>1時間あたり +{(rr['1時間あたり'] - 1) * 100:.1f}% を72時間休まず続ける必要</li>"
                     f"<li>全額をレバレッジ2倍で3日持つなら、BTC が約 {res['hold_price_multiple']:.1f} 倍になる必要</li>")
        pj = res.get("projection")
        if pj:
            need = pj["必要な勝率"]
            parts.append(f"<li>この成績（勝率 {_pct(o['win_rate'])}、1日 {o['trades_per_hour'] * 24:.0f} 回）で"
                         f"資金いっぱい（2倍）で3日続けると、中央値は {pj['中央値の倍率']:.3g} 倍。"
                         f"100倍には勝率 {'100%を超える値' if need > 1 else f'{need * 100:.1f}%'} が必要</li>")
        parts.append("</ul>")
    if model and model.get("rows"):
        rows = []
        for r in model["rows"]:
            rows.append(f"<tr><td>{e(r['scenario'])}</td><td>{_f(r['ic'], 3)}</td><td>{e(r['variant'])}</td>"
                        f"<td>{r['trips_per_day']:.0f}</td><td>{_pct(r['win'])}</td>"
                        f"<td class='{_cls(r['pnl_med'])}'>{r['pnl_med']:+,.0f}</td>"
                        f"<td>{r['profitable']}/{r['n']}</td></tr>")
        parts.append("<h2>模擬相場（予測できるクセの強さ別）</h2>"
                     f"<p class='note'>{e(model.get('note', ''))}</p><div class='scroll'><table>"
                     "<tr><th>クセ</th><th>予測力</th><th>設定</th><th>回/日</th><th>勝率</th><th>3日の損益(円)</th>"
                     "<th>黒字</th></tr>" + "".join(rows) + "</table></div>")
    if history:
        rows = "".join(f"<tr><td>{e(h['jst'])}</td><td>{h['hours']:.1f}h</td><td>{h['trades']}</td>"
                       f"<td class='{_cls(h['avg_bps'])}'>{_f(h['avg_bps'])}</td><td>{e(h['label'])}</td></tr>"
                       for h in history)
        parts.append("<h2>これまでの結果</h2><div class='scroll'><table><tr><th>日時</th><th>記録</th>"
                     "<th>回</th><th>bps/回</th><th>判定</th></tr>" + rows + "</table></div>")
    link = f" ・ <a href='{e(res['run_url'])}'>この実行の記録</a>" if res.get("run_url") else ""
    parts.append("<footer>データ: bitFlyer 公式 Realtime API（記録は GitHub Actions 上）。API キーは使っていません。"
                 "過去のデータでの模擬売買で、将来の成績を保証するものではありません。投資の助言ではありません。<br>"
                 f"<a href='https://github.com/Githitosh/coin1'>プログラム（GitHub）</a>{link}</footer>")
    return ("<!DOCTYPE html><html lang='ja'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>coin1 — 儲かる？シミュレーション</title><style>" + CSS + "</style></head><body><main>"
            + "".join(parts) + "</main></body></html>")
