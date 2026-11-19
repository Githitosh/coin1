"""テスト用の偽取引所（ローカルだけで動く。bitFlyer には接続しない）

- WebSocket（JSON-RPC 2.0）: auth の署名検証、subscribe、板・約定の配信、注文イベントの配信
- HTTP API: 署名検証（クエリ込みのパス）、発注・取消・建玉・証拠金・注文一覧
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.server
import json
import random
import socket
import threading
import time

from bfbot.util import iso_utc
from bfbot.ws import GUID, OP_CLOSE, OP_PING, OP_PONG, OP_TEXT, encode_frame, try_decode_frame

KEY, SECRET = "testkey", "testsecret"
PC = "FX_BTC_JPY"


class WSConn:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buf = bytearray()
        self.lock = threading.Lock()
        self.alive = True
        self.channels: set = set()
        self.authed = False
        self.unmasked_from_client = 0

    def handshake(self) -> None:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("closed")
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        key = ""
        for ln in head.decode().split("\r\n"):
            if ln.lower().startswith("sec-websocket-key:"):
                key = ln.split(":", 1)[1].strip()
        acc = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        self.sock.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Accept: {acc}\r\n\r\n").encode())
        self.buf += rest
        self.sock.settimeout(0.2)

    def send_json(self, obj) -> None:
        frame = encode_frame(OP_TEXT, json.dumps(obj).encode(), mask=False)
        with self.lock:
            try:
                self.sock.sendall(frame)
            except OSError:
                self.alive = False

    def recv_frames(self):
        out = []
        try:
            chunk = self.sock.recv(65536)
            if not chunk:
                self.alive = False
                return out
            self.buf += chunk
        except (socket.timeout, TimeoutError):
            pass
        except OSError:
            self.alive = False
            return out
        while True:
            if len(self.buf) >= 2 and not (self.buf[1] & 0x80):
                self.unmasked_from_client += 1
            fr = try_decode_frame(self.buf)
            if fr is None:
                break
            fin, op, payload, used = fr
            del self.buf[:used]
            out.append((op, payload))
        return out


class FakeExchange:
    def __init__(self, seed: int = 1, race_every: int = 3):
        self.rng = random.Random(seed)
        self.mid = 15_000_000.0
        self.position = 0.0
        self.avg = 0.0
        self.collateral = 100_000.0
        self.realized = 0.0
        self.orders: dict = {}
        self.n_orders = 0
        self.bad_sigs = 0
        self.good_sigs = 0
        self.order_requests: list = []
        self.cancel_all_calls = 0
        self.conns: list = []
        self.lock = threading.RLock()
        self.race_every = race_every
        self.exec_id = 1
        self.running = True

    # ------------------------------------------------------------------ 起動
    def start(self):
        self.ws_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.ws_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.ws_sock.bind(("127.0.0.1", 0))
        self.ws_sock.listen(16)
        self.ws_port = self.ws_sock.getsockname()[1]
        threading.Thread(target=self._accept_loop, daemon=True).start()
        threading.Thread(target=self._market_loop, daemon=True).start()
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self))
        self.http_port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.running = False
        try:
            self.httpd.shutdown()
        except Exception:
            pass
        try:
            self.ws_sock.close()
        except Exception:
            pass
        for c in list(self.conns):
            try:
                c.sock.close()
            except Exception:
                pass

    @property
    def ws_url(self):
        return f"ws://127.0.0.1:{self.ws_port}/json-rpc"

    @property
    def rest_base(self):
        return f"http://127.0.0.1:{self.http_port}"

    # ------------------------------------------------------------ WebSocket
    def _accept_loop(self):
        while self.running:
            try:
                c, _ = self.ws_sock.accept()
            except OSError:
                return
            conn = WSConn(c)
            threading.Thread(target=self._conn_loop, args=(conn,), daemon=True).start()

    def _conn_loop(self, conn: WSConn):
        try:
            conn.handshake()
        except Exception:
            return
        with self.lock:
            self.conns.append(conn)
        while conn.alive and self.running:
            for op, payload in conn.recv_frames():
                if op == OP_TEXT:
                    self._rpc(conn, json.loads(payload))
                elif op == OP_PING:
                    with conn.lock:
                        conn.sock.sendall(encode_frame(OP_PONG, payload, mask=False))
                elif op == OP_CLOSE:
                    conn.alive = False
        with self.lock:
            if conn in self.conns:
                self.conns.remove(conn)

    def _rpc(self, conn: WSConn, req: dict):
        m, rid = req.get("method"), req.get("id")
        if m == "auth":
            p = req.get("params", {})
            sig = hmac.new(SECRET.encode(), f"{p.get('timestamp')}{p.get('nonce')}".encode(),
                           hashlib.sha256).hexdigest()
            ok = (p.get("api_key") == KEY and p.get("signature") == sig
                  and 16 <= len(str(p.get("nonce", ""))) <= 255 and len(str(p.get("timestamp"))) in (10, 13))
            conn.authed = ok
            if ok:
                conn.send_json({"jsonrpc": "2.0", "id": rid, "result": True})
            else:
                conn.send_json({"jsonrpc": "2.0", "id": rid, "error": {"code": -1, "message": "auth"}})
        elif m == "subscribe":
            ch = req.get("params", {}).get("channel")
            if ch == "child_order_events" and not conn.authed:
                conn.send_json({"jsonrpc": "2.0", "id": rid, "error": {"code": -2, "message": "no auth"}})
                return
            conn.channels.add(ch)
            conn.send_json({"jsonrpc": "2.0", "id": rid, "result": True})
            if ch.startswith("lightning_board_snapshot_"):
                self._send(conn, ch, self._snapshot())

    def _send(self, conn, ch, msg):
        conn.send_json({"jsonrpc": "2.0", "method": "channelMessage", "params": {"channel": ch, "message": msg}})

    def _levels(self):
        bb, ba = round(self.mid - 200), round(self.mid + 200)
        bids = [{"price": bb - 100 * i, "size": 0.5} for i in range(10)]
        asks = [{"price": ba + 100 * i, "size": 0.5} for i in range(10)]
        return bids, asks

    def _snapshot(self):
        bids, asks = self._levels()
        return {"mid_price": round(self.mid), "bids": bids, "asks": asks}

    def best(self):
        return round(self.mid - 200), round(self.mid + 200)

    def _market_loop(self):
        last_snap = 0.0
        while self.running:
            time.sleep(0.05)
            with self.lock:
                self.mid += self.rng.choice([-150, -50, 0, 50, 150])
                conns = list(self.conns)
                snap = self._snapshot()
                side = self.rng.choice(["BUY", "SELL"])
                bb, ba = self.best()
                ex = [{"id": self.exec_id, "side": side, "price": ba if side == "BUY" else bb, "size": 0.01,
                       "exec_date": iso_utc(time.time()), "buy_child_order_acceptance_id": "X",
                       "sell_child_order_acceptance_id": "Y"}]
                self.exec_id += 1
            now = time.time()
            for c in conns:
                if f"lightning_board_snapshot_{PC}" in c.channels and now - last_snap > 1.0:
                    self._send(c, f"lightning_board_snapshot_{PC}", snap)
                if f"lightning_board_{PC}" in c.channels:
                    self._send(c, f"lightning_board_{PC}", {"mid_price": snap["mid_price"],
                                                            "bids": snap["bids"][:3], "asks": snap["asks"][:3]})
                if f"lightning_executions_{PC}" in c.channels:
                    self._send(c, f"lightning_executions_{PC}", ex)
            if now - last_snap > 1.0:
                last_snap = now

    def push_events(self, events):
        with self.lock:
            conns = [c for c in self.conns if "child_order_events" in c.channels]
        for c in conns:
            self._send(c, "child_order_events", events)

    # --------------------------------------------------------------- 発注
    def _apply_fill(self, side, qty, px):
        signed = qty if side == "BUY" else -qty
        pos = self.position
        if abs(pos) < 1e-12 or (pos > 0) == (signed > 0):
            new = pos + signed
            self.avg = (self.avg * abs(pos) + px * qty) / abs(new)
            self.position = round(new, 8)
        else:
            close = min(abs(pos), qty)
            self.realized += close * (px - self.avg) * (1 if pos > 0 else -1)
            new = round(pos + signed, 8)
            if abs(new) < 1e-12:
                self.position, self.avg = 0.0, 0.0
            elif (new > 0) != (pos > 0):
                self.position, self.avg = new, px
            else:
                self.position = new

    def handle_order(self, body: dict):
        with self.lock:
            self.n_orders += 1
            acc = f"JRF20261119-TEST-{self.n_orders:06d}"
            self.order_requests.append(body)
            bb, ba = self.best()
            side, size, price = body["side"], float(body["size"]), float(body["price"])
            marketable = (side == "BUY" and price >= ba) or (side == "SELL" and price <= bb)
            fill_px = ba if side == "BUY" else bb
            o = {"child_order_acceptance_id": acc, "child_order_id": f"C{self.n_orders}", "side": side,
                 "size": size, "price": price, "executed_size": 0.0, "average_price": 0.0,
                 "child_order_state": "ACTIVE"}
            self.orders[acc] = o
        events = [{"product_code": PC, "child_order_id": o["child_order_id"], "child_order_acceptance_id": acc,
                   "event_date": iso_utc(time.time()), "event_type": "ORDER", "child_order_type": "LIMIT",
                   "side": side, "price": price, "size": size, "expire_date": iso_utc(time.time() + 60)}]
        if marketable:
            with self.lock:
                self._apply_fill(side, size, fill_px)
                o["executed_size"], o["average_price"], o["child_order_state"] = size, fill_px, "COMPLETED"
                eid = self.exec_id
                self.exec_id += 1
            events.append({"product_code": PC, "child_order_id": o["child_order_id"],
                           "child_order_acceptance_id": acc, "event_date": iso_utc(time.time()),
                           "event_type": "EXECUTION", "exec_id": eid, "side": side, "price": fill_px,
                           "size": size, "commission": 0, "sfd": 0, "outstanding_size": 0})
        elif body.get("time_in_force") == "IOC":
            with self.lock:
                o["child_order_state"] = "CANCELED"
            events.append({"product_code": PC, "child_order_id": o["child_order_id"],
                           "child_order_acceptance_id": acc, "event_date": iso_utc(time.time()),
                           "event_type": "CANCEL", "price": price, "size": size})
        return acc, events


def _make_handler(ex: FakeExchange):
    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _reply(self, code, obj):
            raw = json.dumps(obj).encode() if obj is not None else b""
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _auth_ok(self, body: str) -> bool:
            ts = self.headers.get("ACCESS-TIMESTAMP", "")
            sig = hmac.new(SECRET.encode(), (ts + self.command + self.path + body).encode(),
                           hashlib.sha256).hexdigest()
            ok = self.headers.get("ACCESS-KEY") == KEY and self.headers.get("ACCESS-SIGN") == sig
            with ex.lock:
                if ok:
                    ex.good_sigs += 1
                else:
                    ex.bad_sigs += 1
            return ok

        def do_GET(self):
            path = self.path.split("?")[0]
            if path.startswith("/v1/me/") and not self._auth_ok(""):
                return self._reply(401, {"status": -500, "error_message": "Invalid signature", "data": None})
            q = {}
            if "?" in self.path:
                for kv in self.path.split("?", 1)[1].split("&"):
                    k, _, v = kv.partition("=")
                    q[k] = v
            with ex.lock:
                if path == "/v1/getboardstate":
                    return self._reply(200, {"health": "NORMAL", "state": "RUNNING"})
                if path == "/v1/getticker":
                    bb, ba = ex.best()
                    return self._reply(200, {"product_code": PC, "best_bid": bb, "best_ask": ba})
                if path == "/v1/me/getcollateral":
                    return self._reply(200, {"collateral": ex.collateral + ex.realized, "open_position_pnl": 0,
                                             "require_collateral": 0, "keep_rate": 0})
                if path == "/v1/me/getpositions":
                    if abs(ex.position) < 1e-12:
                        return self._reply(200, [])
                    return self._reply(200, [{"product_code": PC, "side": "BUY" if ex.position > 0 else "SELL",
                                              "price": ex.avg, "size": abs(ex.position)}])
                if path == "/v1/me/getchildorders":
                    acc = q.get("child_order_acceptance_id")
                    if acc:
                        o = ex.orders.get(acc)
                        return self._reply(200, [o] if o else [])
                    st = q.get("child_order_state")
                    return self._reply(200, [o for o in ex.orders.values()
                                             if st is None or o["child_order_state"] == st])
            return self._reply(404, {"status": -1, "error_message": "not found"})

        def do_POST(self):
            n = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(n).decode() if n else ""
            if not self._auth_ok(body):
                return self._reply(401, {"status": -500, "error_message": "Invalid signature", "data": None})
            data = json.loads(body) if body else {}
            if self.path == "/v1/me/sendchildorder":
                acc, events = ex.handle_order(data)
                race = ex.race_every and ex.n_orders % ex.race_every == 0
                if race:  # 応答より先にイベントを届ける（実際に起こりうる順序）
                    ex.push_events(events)
                    time.sleep(0.05)
                self._reply(200, {"child_order_acceptance_id": acc})
                if not race:
                    threading.Timer(0.03, ex.push_events, args=(events,)).start()
                return
            if self.path == "/v1/me/cancelchildorder":
                return self._reply(200, None)
            if self.path == "/v1/me/cancelallchildorders":
                with ex.lock:
                    ex.cancel_all_calls += 1
                return self._reply(200, None)
            return self._reply(404, {"status": -1, "error_message": "not found"})

    return H
