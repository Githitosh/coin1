"""「3日で100倍」がどれだけ現実的かを数字で確かめる

1. 必要な増え方（1日・1時間・1分あたり）
2. 全額をレバレッジで持ち続ける場合に必要な値上がり
3. 高速売買: 全部勝っても何倍になるか / 全勝で100倍に必要な1回の値幅
4. 勝率と値幅から、100倍に届く確率（二項分布で厳密に計算。資金に対し一定割合で張る前提）
5. 記録データがあれば: 未来を完全に知っていても（＝全部当てても）何倍か（動的計画法、コストはスプレッド）
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple


def required_rates(multiple: float = 100.0, days: float = 3.0) -> Dict[str, float]:
    return {
        "1日あたり": multiple ** (1.0 / days),
        "1時間あたり": multiple ** (1.0 / (days * 24.0)),
        "1分あたり": multiple ** (1.0 / (days * 1440.0)),
    }


def hold_price_multiple(multiple: float, leverage: float) -> float:
    """全額をレバレッジ L で持ち続けて資産が multiple 倍になる値上がり倍率（1 + (M-1)/L）"""
    return 1.0 + (multiple - 1.0) / leverage


def all_win_multiple(capture_bps: float, trades_per_day: float, days: float, leverage: float) -> float:
    return (1.0 + leverage * capture_bps / 1e4) ** (trades_per_day * days)


def required_capture_bps(multiple: float, trades_per_day: float, days: float, leverage: float) -> float:
    n = trades_per_day * days
    return (multiple ** (1.0 / n) - 1.0) / leverage * 1e4


def _log_binom_pmf(n: int, k: int, p: float) -> float:
    if p <= 0.0:
        return 0.0 if k == 0 else -math.inf
    if p >= 1.0:
        return 0.0 if k == n else -math.inf
    return (math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
            + k * math.log(p) + (n - k) * math.log1p(-p))


def _log_tail(n: int, kmin: int, p: float) -> float:
    """P(K >= kmin) の自然対数（とても小さい確率も桁あふれせずに扱う）"""
    if kmin <= 0:
        return 0.0
    if kmin > n:
        return -math.inf
    logs = [_log_binom_pmf(n, k, p) for k in range(kmin, n + 1)]
    m = max(logs)
    if m == -math.inf:
        return -math.inf
    return min(0.0, m + math.log(sum(math.exp(x - m) for x in logs)))


def _tail(n: int, kmin: int, p: float) -> float:
    lt = _log_tail(n, kmin, p)
    return 0.0 if lt == -math.inf else math.exp(lt)


def fmt_prob(log_p: float) -> str:
    if log_p == -math.inf:
        return "0（勝ち数が足りない）"
    l10 = log_p / math.log(10)
    if l10 > -4:
        return f"{math.exp(log_p) * 100:.3g}%"
    return f"10^{l10:.0f}"


def prob_reach(p_win: float, win_bps: float, loss_bps: float, trades_per_day: float, days: float,
               leverage: float = 2.0, multiple: float = 100.0, sizing: float = 1.0) -> dict:
    """毎回、資産 × レバレッジ × sizing の建玉で、勝てば +win_bps、負ければ -loss_bps（コスト込み）"""
    n = int(round(trades_per_day * days))
    up = 1.0 + leverage * sizing * win_bps / 1e4
    dn = 1.0 - leverage * sizing * loss_bps / 1e4
    if dn <= 0:
        return {"n": n, "error": "1回の負けで資産がなくなる設定です"}
    lu, ld = math.log(up), math.log(dn)
    need = (math.log(multiple) - n * ld) / (lu - ld)
    kmin = max(0, math.ceil(need - 1e-12))
    half_k = (math.log(0.5) - n * ld) / (lu - ld)  # これ未満の勝ち数だと資産は半分未満
    k_med = int(round(n * p_win))
    log_reach = _log_tail(n, kmin, p_win)
    return {
        "n": n,
        "必要な勝ち数": need,
        "必要な勝率": need / n if n else None,
        "100倍に届く確率": 0.0 if log_reach == -math.inf else math.exp(log_reach),
        "100倍に届く確率_log": log_reach,
        "中央値の倍率": math.exp(k_med * lu + (n - k_med) * ld),
        "全勝の倍率": math.exp(n * lu),
        "半分以下になる確率": 1.0 - _tail(n, max(0, math.ceil(half_k - 1e-12)), p_win),
        "1回あたり期待対数成長": p_win * lu + (1 - p_win) * ld,
    }


def perfect_foresight(times: Sequence[float], mids: Sequence[float], dt: float, leverage: float,
                      half_spread_frac: float) -> Tuple[float, float]:
    """dt 秒ごとに「買い・売り・なし」を未来を知った上で最適に選んだ時の対数倍率（上限）。
    建玉を変えるたびに半スプレッド×変化量のコスト。戻り値: (対数倍率, 期間[時間])"""
    if len(times) < 2:
        return 0.0, 0.0
    grid: List[float] = []
    t0 = times[0]
    k = 0
    for t, m in zip(times, mids):
        while t >= t0 + k * dt:
            grid.append(m)
            k += 1
    L, c = leverage, half_spread_frac
    NEG = -1e300
    V = {-1: NEG, 0: 0.0, 1: NEG}
    for a, b in zip(grid[:-1], grid[1:]):
        r = b / a - 1.0
        nv = {}
        for s2 in (-1, 0, 1):
            best = NEG
            for s1 in (-1, 0, 1):
                if V[s1] <= NEG / 2:
                    continue
                cost = abs(s2 - s1) * L * c
                if cost >= 1.0:
                    continue
                v = V[s1] + math.log1p(-cost)
                if v > best:
                    best = v
            g = 1.0 + s2 * L * r
            nv[s2] = best + math.log(g) if (g > 0 and best > NEG / 2) else NEG
        V = nv
    final = max(V[s] + math.log1p(-abs(s) * L * c) for s in V if V[s] > NEG / 2)
    return final, (len(grid) - 1) * dt / 3600.0


def report(multiple: float = 100.0, days: float = 3.0, leverage: float = 2.0, capital: float = 100000.0,
           scenarios: Optional[List[tuple]] = None, data_paths=None, cfg: Optional[dict] = None,
           verbose: bool = True) -> dict:
    p = print if verbose else (lambda *a, **k: None)
    out: dict = {}
    p(f"\n========== {capital:,.0f} 円を {days:g} 日で {multiple:g} 倍（{capital * multiple:,.0f} 円）にできるか ==========")
    rr = required_rates(multiple, days)
    out["required"] = rr
    p("\n[1] 必要な増え方（複利・休みなし）")
    p(f"  1日あたり ×{rr['1日あたり']:.2f}   1時間あたり +{(rr['1時間あたり'] - 1) * 100:.2f}%"
      f"   1分あたり +{(rr['1分あたり'] - 1) * 100:.3f}%")
    p(f"  → {days * 24:.0f} 時間、1時間も休まず毎時 +{(rr['1時間あたり'] - 1) * 100:.1f}% を続ける必要があります")

    hp = hold_price_multiple(multiple, leverage)
    out["hold_price_multiple"] = hp
    p(f"\n[2] 全額をレバレッジ {leverage:g} 倍で {days:g} 日持ち続ける場合")
    p(f"  BTC が {days:g} 日で約 {hp:.1f} 倍（+{(hp - 1) * 100:,.0f}%）にならないと届きません")
    p("  （bitFlyer Crypto CFD は個人だと最大 2 倍、証拠金率 50%〜。--leverage で変えられます）")

    p(f"\n[3] 高速売買で全部勝った場合（レバレッジ {leverage:g} 倍、毎回全額を回す）")
    caps = (2.0, 5.0, 10.0, 20.0)
    tpds = (100, 500, 2000)
    p("  1回に取る値幅 → " + "  ".join(f"{tp:>5}回/日" for tp in tpds))
    table = {}
    for cb in caps:
        cells = []
        for tp in tpds:
            m = all_win_multiple(cb, tp, days, leverage)
            table[(cb, tp)] = m
            cells.append(f"{m:>9.3g}倍")
        p(f"  {cb:4.0f} bps ({cb / 100:.2f}%)  " + " ".join(cells))
    out["all_win"] = table
    for tp in tpds:
        need = required_capture_bps(multiple, tp, days, leverage)
        p(f"  1日{tp}回で{multiple:g}倍には、毎回コスト後 {need:.2f} bps 以上を一度も負けずに取る必要")

    if scenarios is None:
        scenarios = [(0.52, 5.0, 5.0, 500), (0.55, 5.0, 5.0, 2000), (0.60, 8.0, 6.0, 1000)]
    p(f"\n[4] 勝率と値幅から見た確率（二項分布で厳密計算、レバレッジ {leverage:g} 倍で毎回全額）")
    p("  勝率  勝ち/負け(bps)  回/日 | 必要な勝率 | 100倍の確率 | 中央値 | 全勝 | 半分以下の確率")
    out["scenarios"] = []
    for pw, wb, lb, tp in scenarios:
        r = prob_reach(pw, wb, lb, tp, days, leverage, multiple)
        out["scenarios"].append(((pw, wb, lb, tp), r))
        if "error" in r:
            p(f"  {pw:.2f}  {wb}/{lb}  {tp}: {r['error']}")
            continue
        need = r["必要な勝率"]
        need_s = "不可能(>100%)" if need > 1 else f"{need * 100:.1f}%"
        p(f"  {pw * 100:3.0f}%  {wb:>4g}/{lb:<4g}       {tp:>5} | {need_s:>11} | {fmt_prob(r['100倍に届く確率_log']):>10}"
          f" | {r['中央値の倍率']:6.2f}倍 | {r['全勝の倍率']:8.3g}倍 | {r['半分以下になる確率'] * 100:5.1f}%")
    p("  ※ 勝率・値幅は backtest / paper の結果を --p-win --win-bps --loss-bps --trades-per-day で入れられます")
    p("  ※ 勝率 50% は当てずっぽうと同じです。analyze の「的中率」と比べてください")

    if data_paths and cfg is not None:
        from .analyze import collect_samples
        c = collect_samples(data_paths, cfg, sample_sec=1.0)
        if len(c["t"]) >= 120:
            sp = sorted(c["spread_bps"])
            half = sp[len(sp) // 2] / 2 / 1e4
            p(f"\n[5] 記録データで、未来を完全に知っていた場合（実在しない）の上限"
              f"（コスト = 半スプレッド {half * 1e4:.2f} bps）")
            out["perfect"] = {}
            def fmtx(lg: float) -> str:
                return f"{math.exp(lg):,.3g} 倍" if lg < 50 else f"e^{lg:.0f} 倍"

            for dt in (1.0, 10.0, 60.0):
                lg, hrs = perfect_foresight(c["t"], c["mid"], dt, leverage, half)
                if hrs <= 0:
                    continue
                per72 = lg / hrs * (days * 24.0)
                out["perfect"][dt] = (lg, hrs, per72)
                p(f"  {dt:>4.0f}秒ごとに全部当てた場合: 記録 {hrs:.2f} 時間で {fmtx(lg)}"
                  f" → {days * 24:.0f} 時間に換算すると約 {fmtx(per72)}")
            p("  ※ これは「全部当てる」ことが前提の上限です。実際の予測の当たり方は analyze で確かめてください。")
    return out
