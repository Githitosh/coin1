"""bitFlyer HTTP API の自作クライアント（公式エンドポイントのみ・標準ライブラリのみ）

公式ドキュメント https://lightning.bitflyer.com/docs の仕様に合わせている。
- 認証ヘッダ: ACCESS-KEY / ACCESS-TIMESTAMP / ACCESS-SIGN
- 署名: HMAC-SHA256(API Secret, timestamp + method + path + body) の16進文字列
  GET のクエリはリクエスト行のパスに含まれるので、署名するパスにも含める
  （`python run.py check` で実際の鍵で確認できる）
- 呼び出し上限（公式）: 同一IP 5分500回 / Private API 5分500回 /
  注文系（sendchildorder・sendparentorder・cancelallchildorders）合計 5分300回
  → 手前で少し低めに止める

高速化:
- HTTPS 接続を使い回す（毎回の TLS ハンドシェイクを省く）
- TCP_NODELAY
- 長くアイドルだった接続は発注前に張り直す（切れた接続への発注失敗を避ける）
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import socket
import ssl
import threading
import time
import urllib.parse
from collections import deque
from typing import Any, Dict, List, Optional


class APIError(Exception):
    def __init__(self, status: int, message: str, path: str, body: Any = None):
        super().__init__(f"{path}: HTTP {status} {message}")
        self.status = status
        self.message = message
        self.path = path
        self.body = body


class LocalRateLimit(APIError):
    """手元の呼び出し上限で止めた（取引所には送っていない）"""


class UncertainOrderError(Exception):
    """発注の通信が途中で切れ、取引所が受け付けたか分からない。建玉と注文を照合すること。"""


class SlidingWindowLimiter:
    def __init__(self, max_calls: int, period: float):
        self.max_calls = int(max_calls)
        self.period = float(period)
        self._ts: deque = deque()
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        while self._ts and now - self._ts[0] >= self.period:
            self._ts.popleft()

    def available(self, now: Optional[float] = None) -> int:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._prune(now)
            return self.max_calls - len(self._ts)

    def wait_time(self, now: Optional[float] = None) -> float:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._prune(now)
            if len(self._ts) < self.max_calls:
                return 0.0
            if not self._ts:  # 上限 0（呼び出し禁止）
                return self.period
            return self.period - (now - self._ts[0])

    def take(self, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._ts.append(now)


class BitflyerREST:
    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        base_url: str = "https://api.bitflyer.com",
        timeout: float = 5.0,
        ip_limit=(450, 300.0),
        private_limit=(450, 300.0),
        order_limit=(250, 300.0),
        idle_reconnect_sec: float = 15.0,
        ssl_context: Optional[ssl.SSLContext] = None,
    ):
        u = urllib.parse.urlsplit(base_url)
        if u.scheme not in ("https", "http"):
            raise ValueError("base_url は https:// で始めてください")
        self.scheme = u.scheme
        self.host = u.hostname
        self.port = u.port
        self.api_key = api_key or ""
        self._secret = (api_secret or "").encode("utf-8")
        self.timeout = timeout
        self.ip_limiter = SlidingWindowLimiter(*ip_limit)
        self.private_limiter = SlidingWindowLimiter(*private_limit)
        self.order_limiter = SlidingWindowLimiter(*order_limit)
        self.idle_reconnect_sec = idle_reconnect_sec
        self._ssl_context = ssl_context
        self._conn: Optional[http.client.HTTPConnection] = None
        self._lock = threading.Lock()
        self._last_used = 0.0
        self.connections_opened = 0
        self.last_rtt_ms: Optional[float] = None
        self.rtt_ms: deque = deque(maxlen=500)

    # ------------------------------------------------------------------ 基本
    @property
    def has_keys(self) -> bool:
        return bool(self.api_key and self._secret)

    def sign(self, timestamp: str, method: str, request_path: str, body: str) -> str:
        text = (timestamp + method + request_path + body).encode("utf-8")
        return hmac.new(self._secret, text, hashlib.sha256).hexdigest()

    def _new_conn(self) -> http.client.HTTPConnection:
        if self.scheme == "https":
            ctx = self._ssl_context or ssl.create_default_context()
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                self.host, self.port or 443, timeout=self.timeout, context=ctx)
        else:
            conn = http.client.HTTPConnection(self.host, self.port or 80, timeout=self.timeout)
        conn.connect()
        try:
            conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except (OSError, AttributeError):
            pass
        self.connections_opened += 1
        return conn

    def _reset_conn(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None

    def close(self) -> None:
        with self._lock:
            self._reset_conn()

    def _acquire(self, limiters, wait: bool, path: str) -> None:
        while True:
            now = time.monotonic()
            waits = [lim.wait_time(now) for lim in limiters]
            w = max(waits)
            if w <= 0:
                for lim in limiters:
                    lim.take(now)
                return
            if not wait:
                raise LocalRateLimit(0, f"呼び出し上限の手前で停止（あと{w:.1f}秒）", path)
            time.sleep(min(w, 1.0))

    def request(self, method: str, path: str, query: Optional[Dict[str, Any]] = None,
                body: Optional[Dict[str, Any]] = None, private: bool = False,
                order: bool = False, wait: bool = False, idempotent: Optional[bool] = None) -> Any:
        method = method.upper()
        qs = ""
        if query:
            q = {k: v for k, v in query.items() if v is not None}
            if q:
                qs = "?" + urllib.parse.urlencode(q)
        request_path = path + qs
        body_s = json.dumps(body, separators=(",", ":"), ensure_ascii=False) if body is not None else ""
        limiters = [self.ip_limiter]
        if private:
            limiters.append(self.private_limiter)
        if order:
            limiters.append(self.order_limiter)
        self._acquire(limiters, wait, path)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Connection": "keep-alive",
            "User-Agent": "bfbot-selfmade/1.0",
        }
        if private:
            if not self.has_keys:
                raise APIError(0, "APIキーが未設定です（環境変数 BF_API_KEY / BF_API_SECRET）", path)
            ts = str(int(time.time() * 1000))
            headers["ACCESS-KEY"] = self.api_key
            headers["ACCESS-TIMESTAMP"] = ts
            headers["ACCESS-SIGN"] = self.sign(ts, method, request_path, body_s)
        payload = body_s.encode("utf-8") if body_s else None
        if idempotent is None:
            idempotent = method == "GET"
        with self._lock:
            return self._send(method, request_path, payload, headers, idempotent)

    def _send(self, method, request_path, payload, headers, idempotent):
        if self._conn is not None and time.monotonic() - self._last_used > self.idle_reconnect_sec:
            self._reset_conn()
        attempts = 2 if idempotent else 1
        last_exc: Optional[BaseException] = None
        for _ in range(attempts):
            try:
                if self._conn is None:
                    self._conn = self._new_conn()
                t0 = time.perf_counter()
                self._conn.request(method, request_path, body=payload, headers=headers)
                resp = self._conn.getresponse()
                raw = resp.read()
                rtt = (time.perf_counter() - t0) * 1000.0
                self._last_used = time.monotonic()
                self.last_rtt_ms = rtt
                self.rtt_ms.append(rtt)
                if (resp.getheader("Connection") or "").lower() == "close":
                    self._reset_conn()
                return self._parse(resp.status, raw, request_path)
            except (http.client.HTTPException, OSError) as e:
                self._reset_conn()
                last_exc = e
                if not idempotent:
                    raise UncertainOrderError(f"{request_path}: 通信が途中で切れました（{e!r}）") from e
        raise APIError(0, f"通信エラー: {last_exc!r}", request_path)

    @staticmethod
    def _parse(status: int, raw: bytes, path: str) -> Any:
        text = raw.decode("utf-8", "replace") if raw else ""
        data: Any = None
        if text:
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = text
        # 公式: status が負の値なら処理は確定していない
        if isinstance(data, dict) and isinstance(data.get("status"), int) and data["status"] < 0:
            raise APIError(status, f"status={data['status']} {data.get('error_message', '')}", path, data)
        if status != 200:
            msg = data.get("error_message") if isinstance(data, dict) else text[:200]
            raise APIError(status, str(msg), path, data)
        return data

    # ------------------------------------------------------------ Public API
    def get_markets(self) -> List[dict]:
        return self.request("GET", "/v1/getmarkets", wait=True)

    def get_board(self, product_code: str) -> dict:
        return self.request("GET", "/v1/getboard", {"product_code": product_code}, wait=True)

    def get_ticker(self, product_code: str) -> dict:
        return self.request("GET", "/v1/getticker", {"product_code": product_code}, wait=True)

    def get_executions(self, product_code: str, count: int = 100, before=None, after=None) -> List[dict]:
        return self.request("GET", "/v1/getexecutions",
                            {"product_code": product_code, "count": count, "before": before, "after": after},
                            wait=True)

    def get_health(self, product_code: str) -> dict:
        return self.request("GET", "/v1/gethealth", {"product_code": product_code}, wait=True)

    def get_boardstate(self, product_code: str) -> dict:
        return self.request("GET", "/v1/getboardstate", {"product_code": product_code}, wait=True)

    # ----------------------------------------------------------- Private API
    def get_balance(self) -> List[dict]:
        return self.request("GET", "/v1/me/getbalance", private=True, wait=True)

    def get_collateral(self) -> dict:
        return self.request("GET", "/v1/me/getcollateral", private=True, wait=True)

    def get_positions(self, product_code: str) -> List[dict]:
        return self.request("GET", "/v1/me/getpositions", {"product_code": product_code},
                            private=True, wait=True)

    def get_child_orders(self, product_code: str, **params) -> List[dict]:
        q = {"product_code": product_code}
        q.update(params)
        return self.request("GET", "/v1/me/getchildorders", q, private=True, wait=True)

    def get_my_executions(self, product_code: str, **params) -> List[dict]:
        q = {"product_code": product_code}
        q.update(params)
        return self.request("GET", "/v1/me/getexecutions", q, private=True, wait=True)

    def send_child_order(self, product_code: str, side: str, size: float, child_order_type: str = "LIMIT",
                         price: Optional[float] = None, minute_to_expire: Optional[int] = None,
                         time_in_force: Optional[str] = None) -> dict:
        if side not in ("BUY", "SELL"):
            raise ValueError("side は BUY か SELL")
        if child_order_type not in ("LIMIT", "MARKET"):
            raise ValueError("child_order_type は LIMIT か MARKET")
        body: Dict[str, Any] = {
            "product_code": product_code,
            "child_order_type": child_order_type,
            "side": side,
            "size": round(float(size), 8),
        }
        if child_order_type == "LIMIT":
            if price is None:
                raise ValueError("LIMIT 注文には price が必要")
            p = float(price)
            body["price"] = int(p) if p.is_integer() else p
        if minute_to_expire:
            body["minute_to_expire"] = int(minute_to_expire)
        if time_in_force:
            body["time_in_force"] = time_in_force
        # 発注は二重送信を避けるため自動再送しない（通信断は UncertainOrderError）
        return self.request("POST", "/v1/me/sendchildorder", body=body, private=True, order=True,
                            idempotent=False)

    def cancel_child_order(self, product_code: str, child_order_acceptance_id: Optional[str] = None,
                           child_order_id: Optional[str] = None) -> Any:
        body: Dict[str, Any] = {"product_code": product_code}
        if child_order_acceptance_id:
            body["child_order_acceptance_id"] = child_order_acceptance_id
        elif child_order_id:
            body["child_order_id"] = child_order_id
        else:
            raise ValueError("取り消す注文の ID が必要")
        # 取消は二度送っても害がないので再送してよい
        return self.request("POST", "/v1/me/cancelchildorder", body=body, private=True, idempotent=True)

    def cancel_all_child_orders(self, product_code: str) -> Any:
        return self.request("POST", "/v1/me/cancelallchildorders", body={"product_code": product_code},
                            private=True, order=True, idempotent=True, wait=True)
