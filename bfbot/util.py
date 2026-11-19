"""時刻・ログ・記録ファイルの小道具（標準ライブラリのみ）"""

from __future__ import annotations

import datetime as _dt
import gzip
import json
import logging
import os
import time
from typing import Iterable, Iterator, List, Optional

JST = _dt.timezone(_dt.timedelta(hours=9))
log = logging.getLogger("bfbot")


def jst_str(ts: Optional[float] = None) -> str:
    if ts is None:
        ts = time.time()
    return _dt.datetime.fromtimestamp(ts, JST).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def jst_date(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, JST).strftime("%Y-%m-%d")


def iso_utc(ts: float) -> str:
    """UNIX 秒 → bitFlyer と同じ形式の ISO 8601（UTC, 末尾 Z）"""
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_exec_date(s: str) -> float:
    """bitFlyer の時刻文字列（例 2015-07-07T10:44:33.547Z、小数部の桁数は可変、Z なしも UTC）を UNIX 秒へ。"""
    if not s:
        return 0.0
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1]
    frac = 0.0
    if "." in s:
        s, f = s.split(".", 1)
        digits = "".join(ch for ch in f if ch.isdigit())
        if digits:
            frac = int(digits) / (10 ** len(digits))
    try:
        base = _dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        return 0.0
    return base.timestamp() + frac


def setup_logging(log_path: Optional[str] = None, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("bfbot")
    logger.setLevel(level)
    if logger.handlers:
        return logger
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fmt.converter = lambda secs: _dt.datetime.fromtimestamp(secs, JST).timetuple()
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    if log_path:
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger


class JsonlRecorder:
    """受信データを gzip 圧縮の JSON Lines で保存する。日本時間の1時間ごとにファイルを分ける。"""

    def __init__(self, directory: str, prefix: str):
        self.directory = directory
        self.prefix = prefix
        os.makedirs(directory, exist_ok=True)
        self._fh = None
        self._key = None
        self.count = 0

    def _open_for(self, ts: float) -> None:
        key = _dt.datetime.fromtimestamp(ts, JST).strftime("%Y%m%d_%H")
        if key != self._key:
            self.close()
            path = os.path.join(self.directory, f"{self.prefix}_{key}.jsonl.gz")
            self._fh = gzip.open(path, "at", encoding="utf-8")
            self._key = key

    def write(self, obj: dict, ts: float) -> None:
        self._open_for(ts)
        self._fh.write(json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n")
        self.count += 1

    def flush(self) -> None:
        if self._fh is not None:
            self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None
                self._key = None


def expand_paths(paths: Iterable[str]) -> List[str]:
    out: List[str] = []
    for p in paths:
        if os.path.isdir(p):
            for name in sorted(os.listdir(p)):
                if name.endswith(".jsonl") or name.endswith(".jsonl.gz"):
                    out.append(os.path.join(p, name))
        elif os.path.exists(p):
            out.append(p)
    return out


def iter_records(paths: Iterable[str]) -> Iterator[dict]:
    """記録ファイルを時刻順（ファイル名順）に読む。記録を途中で止めた時の壊れた末尾は読み飛ばす。"""
    for path in expand_paths(paths):
        opener = gzip.open if path.endswith(".gz") else open
        try:
            with opener(path, "rt", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
        except (EOFError, OSError) as e:
            log.warning(f"{path}: 末尾が壊れていたので途中まで読みました（{e}）")
