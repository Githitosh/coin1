"""板と約定を記録する（分析・バックテスト用）"""

from __future__ import annotations

import logging
import signal
import threading
import time
from typing import Optional

from .util import JsonlRecorder
from .ws import RealtimeClient

log = logging.getLogger("bfbot")


def run_recorder(product_code: str, out_dir: str, hours: Optional[float] = None,
                 ws_url: Optional[str] = None) -> int:
    rec = JsonlRecorder(out_dir, product_code)
    lock = threading.Lock()

    def on_msg(ch, m, t):
        with lock:
            rec.write({"t": t, "ch": ch, "m": m}, t)

    def on_disc():
        t = time.time()
        with lock:
            rec.write({"t": t, "ch": "_disconnect", "m": None}, t)  # 再生時に板を作り直す目印

    channels = [f"lightning_board_snapshot_{product_code}", f"lightning_board_{product_code}",
                f"lightning_executions_{product_code}"]
    client = RealtimeClient(channels, on_msg, url=ws_url, name="record", on_disconnect=on_disc)
    stop = threading.Event()
    old = signal.signal(signal.SIGINT, lambda *_: stop.set())
    client.start()
    t_end = time.time() + hours * 3600 if hours else None
    last = 0.0
    log.info(f"記録開始: {product_code} → {out_dir}（Ctrl+C で終了）")
    try:
        while not stop.is_set():
            stop.wait(1.0)
            now = time.time()
            if t_end and now >= t_end:
                break
            if now - last >= 60:
                last = now
                with lock:
                    rec.flush()
                log.info(f"記録 {rec.count:,} 件")
    finally:
        signal.signal(signal.SIGINT, old)
        client.stop()
        with lock:
            rec.close()
    log.info(f"記録終了: {rec.count:,} 件")
    return rec.count
