#!/usr/bin/env python3
"""bitFlyer 高速bot のコマンド

  python run.py record   [--hours 6]             板と約定を記録（data/ に保存）
  python run.py analyze  data/                   傾向分析（指標が値動きを当てるか、コスト込みで）
  python run.py backtest data/                   記録データで売買を再現
  python run.py paper    [--minutes 60]          実データで模擬売買（お金は動かない）
  python run.py target   [--data data/]          「3日で100倍」を数字で検証
  python run.py check                            APIキーの確認（読み取りだけ・発注しない）
  python run.py live --i-understand-real-money   本番（3重ロックを全部外した時だけ）
  python run.py synth                            動作確認用の合成データを作る
  python run.py simulate [--all]                 模擬相場で「儲かるか」を試す（予測力の強さ別）
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

from bfbot.config import load_config
from bfbot.util import JST, setup_logging


def _logpath(cfg, name):
    stamp = dt.datetime.now(JST).strftime("%Y%m%d_%H%M%S")
    return os.path.join(cfg.get("log_dir", "logs"), f"{name}_{stamp}.log")


def cmd_record(args, cfg):
    from bfbot.recorder import run_recorder
    setup_logging(_logpath(cfg, "record"))
    run_recorder(cfg["product_code"], args.out, hours=args.hours)


def cmd_analyze(args, cfg):
    from bfbot.analyze import analyze
    setup_logging()
    horizons = tuple(float(x) if "." in x else int(x) for x in args.horizons.split(","))
    out_csv = args.csv or os.path.join(cfg.get("log_dir", "logs"), "analyze.csv")
    analyze(args.paths, cfg, horizons=horizons, sample_sec=args.sample, out_csv=out_csv)


def cmd_backtest(args, cfg):
    from bfbot.engine import run_backtest
    setup_logging()
    eng = run_backtest(cfg, args.paths, quiet=not args.verbose)
    eng.print_summary()
    path = os.path.join(cfg.get("log_dir", "logs"), "backtest_trades.csv")
    eng.save_trades(path)
    print(f"約定の一覧: {path}")
    print("※ 過去データでの結果です。手数料・遅延・約定の判定は控えめにしていますが、将来の成績は保証されません。")


def cmd_paper(args, cfg):
    from bfbot.engine import run_realtime
    setup_logging(_logpath(cfg, "paper"))
    eng = run_realtime(cfg, "paper", minutes=args.minutes)
    eng.print_summary()
    eng.save_trades(os.path.join(cfg.get("log_dir", "logs"), "paper_trades.csv"))


def cmd_live(args, cfg):
    from bfbot.engine import run_realtime
    locks = []
    if not cfg["live"].get("enabled"):
        locks.append('config.json の "live": {"enabled": true}')
    if os.environ.get("BF_LIVE") != "1":
        locks.append("環境変数 BF_LIVE=1")
    if not args.i_understand_real_money:
        locks.append("コマンドの --i-understand-real-money")
    if locks:
        print("本番発注のロックが残っています（実際のお金が動くため3つとも必要）:")
        for x in locks:
            print("  -", x)
        print("先に paper で動きを確かめてください。")
        sys.exit(2)
    key, secret = os.environ.get("BF_API_KEY", ""), os.environ.get("BF_API_SECRET", "")
    if not (key and secret):
        print("環境変数 BF_API_KEY / BF_API_SECRET を設定してください。")
        sys.exit(2)
    setup_logging(_logpath(cfg, "live"))
    eng = run_realtime(cfg, "live", minutes=args.minutes, api_key=key, api_secret=secret, armed=True)
    eng.print_summary()
    eng.save_trades(os.path.join(cfg.get("log_dir", "logs"), "live_trades.csv"))


def cmd_check(args, cfg):
    from bfbot.rest import APIError, BitflyerREST
    key, secret = os.environ.get("BF_API_KEY", ""), os.environ.get("BF_API_SECRET", "")
    rest = BitflyerREST(key, secret)
    pc = cfg["product_code"]
    try:
        st = rest.get_boardstate(pc)
        print(f"板の状態 {pc}: {st}")
        tk = rest.get_ticker(pc)
        print(f"最良気配: 買 {tk.get('best_bid')} / 売 {tk.get('best_ask')}")
    except APIError as e:
        print(f"公開APIに接続できません: {e}")
        return
    if not (key and secret):
        print("APIキーが未設定なので、ここまで（BF_API_KEY / BF_API_SECRET を設定すると残高も確認します）")
        return
    try:
        col = rest.get_collateral()
        print(f"証拠金: {col.get('collateral')} 円 / 維持率 {col.get('keep_rate')}")
        pos = rest.get_positions(pc)
        print(f"建玉: {len(pos)} 件（クエリ付き GET の署名も正常）")
    except APIError as e:
        print(f"認証付きAPIでエラー: {e}")
        print("キーの権限（残高・建玉の参照）と、PC の時刻が合っているかを確認してください。")
        return
    rtt = sorted(rest.rtt_ms)
    if rtt:
        print(f"REST の往復時間 中央値 {rtt[len(rtt) // 2]:.0f} ms（{len(rtt)} 回）")


def cmd_target(args, cfg):
    from bfbot.target import report
    scenarios = None
    if args.p_win is not None:
        scenarios = [(args.p_win, args.win_bps, args.loss_bps, args.trades_per_day)]
    report(multiple=args.multiple, days=args.days, leverage=args.leverage, capital=args.capital,
           scenarios=scenarios, data_paths=args.data, cfg=cfg)


def cmd_simulate(args, cfg):
    from bfbot.simulate import SCENARIOS, report, run_all
    sc = SCENARIOS + [("とても強い", 0.25), ("極端", 0.5)] if args.all else SCENARIOS
    print(f"模擬相場で {args.days:g} 日 × {args.seeds} 通り × {len(sc)} ケースを実行します（実際の相場ではありません）")
    res = run_all(days=args.days, seeds=args.seeds, workers=args.workers, scenarios=sc,
                  market_kw={"daily_vol": args.daily_vol, "spread_bps": args.spread_bps})
    report(res, capital=float(cfg["capital_jpy"]))
    print("\n※ 予測力IC は bot の合成スコアと30秒後の値動きの相関。実データの値は analyze で測れます。")


def cmd_synth(args, cfg):
    from bfbot.synth import generate
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    n = generate(args.out, minutes=args.minutes, seed=args.seed, product=cfg["product_code"])
    print(f"合成データ {n:,} 件 → {args.out}（実際の相場ではありません）")


def main(argv=None):
    ap = argparse.ArgumentParser(description="bitFlyer 高速bot（公式APIのみ・標準ライブラリのみ）")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--product", help="FX_BTC_JPY（既定）または BTC_JPY")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("record")
    s.add_argument("--out", default="data")
    s.add_argument("--hours", type=float)
    s.set_defaults(func=cmd_record)

    s = sub.add_parser("analyze")
    s.add_argument("paths", nargs="+")
    s.add_argument("--horizons", default="1,5,10,30,60")
    s.add_argument("--sample", type=float, default=0.5)
    s.add_argument("--csv")
    s.set_defaults(func=cmd_analyze)

    s = sub.add_parser("backtest")
    s.add_argument("paths", nargs="+")
    s.add_argument("--verbose", action="store_true")
    s.set_defaults(func=cmd_backtest)

    s = sub.add_parser("paper")
    s.add_argument("--minutes", type=float)
    s.set_defaults(func=cmd_paper)

    s = sub.add_parser("live")
    s.add_argument("--minutes", type=float)
    s.add_argument("--i-understand-real-money", action="store_true")
    s.set_defaults(func=cmd_live)

    s = sub.add_parser("check")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("target")
    s.add_argument("--data", nargs="*")
    s.add_argument("--multiple", type=float, default=100.0)
    s.add_argument("--days", type=float, default=3.0)
    s.add_argument("--leverage", type=float, default=2.0)
    s.add_argument("--capital", type=float, default=100000.0)
    s.add_argument("--p-win", type=float)
    s.add_argument("--win-bps", type=float, default=5.0)
    s.add_argument("--loss-bps", type=float, default=5.0)
    s.add_argument("--trades-per-day", type=float, default=500.0)
    s.set_defaults(func=cmd_target)

    s = sub.add_parser("simulate")
    s.add_argument("--days", type=float, default=3.0)
    s.add_argument("--seeds", type=int, default=3)
    s.add_argument("--workers", type=int, default=2)
    s.add_argument("--daily-vol", type=float, default=0.02)
    s.add_argument("--spread-bps", type=float, default=1.8)
    s.add_argument("--all", action="store_true", help="予測力がとても強い・極端なケースも入れる")
    s.set_defaults(func=cmd_simulate)

    s = sub.add_parser("synth")
    s.add_argument("--out", default="data_synth/synth.jsonl.gz")
    s.add_argument("--minutes", type=float, default=60.0)
    s.add_argument("--seed", type=int, default=7)
    s.set_defaults(func=cmd_synth)

    args = ap.parse_args(argv)
    overrides = {"product_code": args.product} if args.product else None
    cfg = load_config(args.config, overrides)
    args.func(args, cfg)


if __name__ == "__main__":
    main()
