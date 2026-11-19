"""設定の既定値と読み込み"""

from __future__ import annotations

import copy
import dataclasses
import json
import logging
import os
from typing import Optional

log = logging.getLogger("bfbot")

DEFAULT_CONFIG = {
    # FX_BTC_JPY = bitFlyer Crypto CFD（売買手数料無料・売りから入れる・個人は最大2倍）
    # BTC_JPY    = Lightning 現物（手数料あり・売りから入れない）
    "product_code": "FX_BTC_JPY",
    "capital_jpy": 100000,
    "paper": {
        "fee_rate": 0.0,                      # 現物 BTC_JPY を選ぶと 0.0015 に自動変更
        "latency_ms": 60,                     # 注文が取引所に届くまでの想定遅延
        "leverage": 2.0,
        "loss_cut_rate": 0.5,                 # 証拠金維持率がこれを下回ったら強制決済（模擬）
        "leverage_point_rate_per_day": 0.0004  # 18時をまたいだ建玉に 0.04%/日（模擬）
    },
    "features": {
        "tau_fast": 3.0,
        "tau_slow": 30.0,
        "tau_regime_fast": 60.0,
        "tau_regime_slow": 900.0,
        "tau_flow": 5.0,
        "tau_ofi": 3.0,
        "tau_vol": 120.0,
        "obi_levels": 5,
        "w_trend": 1.0,
        "w_flow": 1.0,
        "w_obi": 0.5,
        "w_ofi": 1.0,
        "warmup_sec": 60.0,
        "regime_warmup_sec": 300.0
    },
    "strategy": {
        "size": 0.001,
        "min_size": 0.001,
        "order_mode": "taker",
        "entry_threshold": 0.45,
        "exit_on_reverse": 0.25,
        "take_profit_bps": 8.0,
        "stop_loss_bps": 6.0,
        "max_hold_sec": 45.0,
        "cooldown_sec": 3.0,
        "taker_slippage_bps": 3.0,
        "exit_slippage_bps": 8.0,
        "maker_timeout_sec": 4.0,
        "maker_improve_ticks": 0,
        "max_spread_bps": 4.0,
        "min_vol_bps_1m": 0.5,
        "max_vol_bps_1m": 150.0,
        "allow_short": True,
        "trend_filter": True,
        "tick": 1.0,
        "minute_to_expire": 1
    },
    "risk": {
        "max_position_btc": 0.002,
        "max_daily_loss_jpy": 3000.0,
        "max_drawdown_jpy": 5000.0,
        "max_consecutive_losses": 5,
        "pause_after_losses_sec": 600.0,
        "max_orders_per_min": 20,
        "min_order_interval_sec": 0.5,
        "stale_data_sec": 3.0,
        "max_api_errors_per_5min": 10,
        "allowed_health": ["NORMAL", "BUSY", "VERY BUSY"]
    },
    "live": {
        "enabled": False,
        "flatten_on_exit": True
    },
    "report_sec": 10.0,
    "log_dir": "logs"
}


def deep_merge(base: dict, override: dict) -> dict:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def load_config(path: Optional[str] = None, overrides: Optional[dict] = None) -> dict:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    user: dict = {}
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            user = json.load(fh)
        deep_merge(cfg, user)
    if overrides:
        deep_merge(cfg, overrides)
        deep_merge(user, overrides)
    if cfg["product_code"] == "BTC_JPY":
        # 現物は売りから入れない。手数料は少額だと 0.15%（公式の手数料ページで最新値を確認すること）
        cfg["strategy"]["allow_short"] = False
        if "fee_rate" not in user.get("paper", {}):
            cfg["paper"]["fee_rate"] = 0.0015
    return cfg


def make_dc(cls, d: dict):
    """dict から dataclass を作る。知らないキーは警告して無視する。"""
    names = {f.name for f in dataclasses.fields(cls)}
    unknown = [k for k in d if k not in names]
    if unknown:
        log.warning(f"{cls.__name__}: 使われない設定キー {unknown}")
    return cls(**{k: v for k, v in d.items() if k in names})
