"""WebSocket（RFC 6455）と bitFlyer Realtime API（JSON-RPC 2.0）の自作クライアント

外部ライブラリを使わず socket と ssl だけで書いている。
公式: https://bf-lightning-api.readme.io/docs/endpoint-json-rpc
- 接続先 wss://ws.lightstream.bitflyer.com/json-rpc
- subscribe / unsubscribe / auth を JSON-RPC 2.0 で送る
- 配信は method "channelMessage" の params.channel / params.message
- 認証: HMAC-SHA256(API Secret, f"{timestamp}{nonce}") の16進。timestamp は 13 桁、nonce は 16〜255 文字
- 公式の注意: 認証の応答を確かめてから次の動作をする / 接続と切断を高頻度で繰り返さない
  （再接続は待ち時間を倍々に伸ばす）
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import random
import socket
import ssl
import struct
import threading
import time
import urllib.parse
from typing import Callable, Iterable, List, Optional, Union

log = logging.getLogger("bfbot")

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WebSocketError(Exception):
    pass


class WebSocketClosed(WebSocketError):
    def __init__(self, code: Optional[int] = None, reason: str = ""):
        super().__init__(f"closed code={code} reason={reason}")
        self.code = code
        self.reason = reason


def xor_mask(data: bytes, mask: bytes) -> bytes:
    n = len(data)
    if n == 0:
        return b""
    reps = (mask * ((n + 3) // 4))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(reps, "big")).to_bytes(n, "big")


def encode_frame(opcode: int, payload: bytes, mask: bool = True, fin: bool = True,
                 mask_key: Optional[bytes] = None) -> bytes:
    head = bytearray([(0x80 if fin else 0) | opcode])
    n = len(payload)
    mbit = 0x80 if mask else 0
    if n < 126:
        head.append(mbit | n)
    elif n < 65536:
        head.append(mbit | 126)
        head += struct.pack("!H", n)
    else:
        head.append(mbit | 127)
        head += struct.pack("!Q", n)
    if mask:
        key = mask_key or os.urandom(4)
        head += key
        return bytes(head) + xor_mask(payload, key)
    return bytes(head) + payload


def try_decode_frame(buf: Union[bytearray, bytes]):
    """buf の先頭に完全なフレームがあれば (fin, opcode, payload, 使ったバイト数)。足りなければ None。
    途中までしか届いていないフレームを消費しないので、読み取りのタイムアウトで同期がずれない。"""
    if len(buf) < 2:
        return None
    b1, b2 = buf[0], buf[1]
    fin = bool(b1 & 0x80)
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    n = b2 & 0x7F
    pos = 2
    if n == 126:
        if len(buf) < 4:
            return None
        n = struct.unpack_from("!H", buf, 2)[0]
        pos = 4
    elif n == 127:
        if len(buf) < 10:
            return None
        n = struct.unpack_from("!Q", buf, 2)[0]
        pos = 10
    key = None
    if masked:
        if len(buf) < pos + 4:
            return None
        key = bytes(buf[pos:pos + 4])
        pos += 4
    if len(buf) < pos + n:
        return None
    payload = bytes(buf[pos:pos + n])
    if key:
        payload = xor_mask(payload, key)
    return fin, opcode, payload, pos + n


class WebSocket:
    def __init__(self, url: str, connect_timeout: float = 10.0, read_timeout: float = 1.0,
                 ssl_context: Optional[ssl.SSLContext] = None, max_message_bytes: int = 64 * 1024 * 1024):
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("ws", "wss"):
            raise ValueError("URL は ws:// か wss://")
        self.secure = u.scheme == "wss"
        self.host = u.hostname
        self.port = u.port or (443 if self.secure else 80)
        self.resource = (u.path or "/") + (("?" + u.query) if u.query else "")
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.ssl_context = ssl_context
        self.max_message_bytes = max_message_bytes
        self.sock = None
        self._buf = bytearray()
        self._send_lock = threading.Lock()
        self._frag_op: Optional[int] = None
        self._frags: List[bytes] = []
        self.last_recv = 0.0
        self.last_pong = 0.0
        self.closed = True

    def connect(self) -> None:
        raw = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        try:
            raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if self.secure:
                ctx = self.ssl_context or ssl.create_default_context()
                ctx.minimum_version = ssl.TLSVersion.TLSv1_2  # 公式: TLS 1.2 以上
                sock = ctx.wrap_socket(raw, server_hostname=self.host)
            else:
                sock = raw
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            host_hdr = self.host if self.port in (80, 443) else f"{self.host}:{self.port}"
            req = (f"GET {self.resource} HTTP/1.1\r\nHost: {host_hdr}\r\nUpgrade: websocket\r\n"
                   f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                   f"User-Agent: bfbot-selfmade/1.0\r\n\r\n")
            sock.sendall(req.encode("ascii"))
            buf = bytearray()
            deadline = time.monotonic() + self.connect_timeout
            while b"\r\n\r\n" not in buf:
                if time.monotonic() > deadline:
                    raise WebSocketError("ハンドシェイクがタイムアウト")
                chunk = sock.recv(4096)
                if not chunk:
                    raise WebSocketError("ハンドシェイク中に切断された")
                buf += chunk
                if len(buf) > 65536:
                    raise WebSocketError("ハンドシェイクの応答が大きすぎる")
            head, _, rest = bytes(buf).partition(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            parts = lines[0].split(" ", 2)
            if len(parts) < 2 or parts[1] != "101":
                raise WebSocketError(f"ハンドシェイク失敗: {lines[0]}")
            hdrs = {}
            for ln in lines[1:]:
                if ":" in ln:
                    k, v = ln.split(":", 1)
                    hdrs[k.strip().lower()] = v.strip()
            expect = base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")
            if hdrs.get("sec-websocket-accept") != expect:
                raise WebSocketError("Sec-WebSocket-Accept が一致しない")
            if hdrs.get("upgrade", "").lower() != "websocket":
                raise WebSocketError("Upgrade ヘッダが websocket ではない")
            sock.settimeout(self.read_timeout)
            self.sock = sock
            self._buf = bytearray(rest)  # 応答と同じパケットで届いたフレームを捨てない
            self.last_recv = time.monotonic()
            self.closed = False
        except Exception:
            try:
                raw.close()
            except Exception:
                pass
            raise

    def _send(self, opcode: int, payload: bytes) -> None:
        frame = encode_frame(opcode, payload, mask=True)  # クライアント→サーバは必ずマスク
        with self._send_lock:
            if self.sock is None:
                raise WebSocketClosed(None, "未接続")
            self.sock.sendall(frame)

    def send_text(self, text: str) -> None:
        self._send(OP_TEXT, text.encode("utf-8"))

    def ping(self, data: bytes = b"") -> None:
        self._send(OP_PING, data)

    def recv_message(self) -> Optional[Union[str, bytes]]:
        """次のメッセージを返す。read_timeout の間に何も来なければ None（定期処理のため）。"""
        while True:
            fr = try_decode_frame(self._buf)
            if fr is None:
                try:
                    chunk = self.sock.recv(262144)
                except (socket.timeout, TimeoutError):
                    return None
                except ssl.SSLWantReadError:
                    return None
                if not chunk:
                    self.closed = True
                    raise WebSocketClosed(None, "EOF")
                self._buf += chunk
                self.last_recv = time.monotonic()
                if len(self._buf) > self.max_message_bytes + 16:
                    raise WebSocketError("フレームが大きすぎる")
                continue
            fin, op, payload, used = fr
            del self._buf[:used]
            if op == OP_PING:
                self._send(OP_PONG, payload)
                continue
            if op == OP_PONG:
                self.last_pong = time.monotonic()
                continue
            if op == OP_CLOSE:
                code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else None
                reason = payload[2:].decode("utf-8", "replace")
                try:
                    self._send(OP_CLOSE, payload[:2])
                except Exception:
                    pass
                self.closed = True
                raise WebSocketClosed(code, reason)
            if op in (OP_TEXT, OP_BIN):
                if fin:
                    return self._finish(op, payload)
                self._frag_op = op
                self._frags = [payload]
                continue
            if op == OP_CONT:
                if self._frag_op is None:
                    raise WebSocketError("想定外の継続フレーム")
                self._frags.append(payload)
                if fin:
                    op0, data = self._frag_op, b"".join(self._frags)
                    self._frag_op, self._frags = None, []
                    return self._finish(op0, data)
                continue
            raise WebSocketError(f"未知の opcode {op}")

    @staticmethod
    def _finish(op: int, data: bytes) -> Union[str, bytes]:
        return data.decode("utf-8") if op == OP_TEXT else data

    def close(self, code: int = 1000) -> None:
        if self.sock is None:
            return
        try:
            self._send(OP_CLOSE, struct.pack("!H", code))
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass
        self.sock = None
        self.closed = True


class RealtimeClient:
    """bitFlyer Realtime API（JSON-RPC 2.0 over WebSocket）。別スレッドで受信し、届いた順に on_message を呼ぶ。"""

    DEFAULT_URL = "wss://ws.lightstream.bitflyer.com/json-rpc"

    def __init__(self, channels: Iterable[str],
                 on_message: Callable[[str, object, float], None], *,
                 url: Optional[str] = None, api_key: str = "", api_secret: str = "",
                 name: str = "public", on_connect: Optional[Callable[[], None]] = None,
                 on_disconnect: Optional[Callable[[], None]] = None,
                 ping_interval: float = 20.0, stale_sec: float = 60.0,
                 ssl_context: Optional[ssl.SSLContext] = None,
                 min_backoff: float = 2.0, max_backoff: float = 60.0):
        self.channels = list(channels)
        self.on_message = on_message
        self.url = url or self.DEFAULT_URL
        self.api_key = api_key or ""
        self._secret = (api_secret or "").encode("utf-8")
        self.name = name
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self.ping_interval = ping_interval
        self.stale_sec = stale_sec
        self.ssl_context = ssl_context
        self.min_backoff = min_backoff
        self.max_backoff = max_backoff
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ws: Optional[WebSocket] = None
        self.connected = threading.Event()
        self.subscribed: set = set()
        self.msg_count = 0
        self.reconnects = 0
        self.callback_errors = 0
        self.last_error = ""

    @property
    def needs_auth(self) -> bool:
        return bool(self.api_key and self._secret)

    def auth_params(self) -> dict:
        ts = int(time.time() * 1000)            # 13 桁
        nonce = os.urandom(16).hex()            # 32 文字
        sig = hmac.new(self._secret, f"{ts}{nonce}".encode("utf-8"), hashlib.sha256).hexdigest()
        return {"api_key": self.api_key, "timestamp": ts, "nonce": nonce, "signature": sig}

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"rt-{self.name}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            ws.close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        backoff = self.min_backoff
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._session()
            except Exception as e:  # 切断・通信エラー・認証失敗
                self.last_error = repr(e)
                if not self._stop.is_set():
                    log.warning(f"[{self.name}] 接続が切れました: {e!r}")
            finally:
                self.connected.clear()
                self.subscribed.clear()
                ws = self._ws
                self._ws = None
                if ws is not None:
                    ws.close()
                if self.on_disconnect is not None:
                    try:
                        self.on_disconnect()
                    except Exception:
                        log.exception(f"[{self.name}] on_disconnect でエラー")
            if self._stop.is_set():
                break
            if time.monotonic() - started > 120:
                backoff = self.min_backoff  # 長く安定していたら待ち時間を戻す
            wait = backoff + random.uniform(0, backoff / 2)
            log.info(f"[{self.name}] {wait:.1f}秒後に再接続します")
            self._stop.wait(wait)
            backoff = min(backoff * 2, self.max_backoff)
            self.reconnects += 1

    def _send_rpc(self, ws: WebSocket, method: str, params: dict, rid: int) -> None:
        ws.send_text(json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": rid},
                                separators=(",", ":")))

    def _session(self) -> None:
        ws = WebSocket(self.url, ssl_context=self.ssl_context)
        ws.connect()
        self._ws = ws
        rid = 0
        if self.needs_auth:
            rid += 1
            self._send_rpc(ws, "auth", self.auth_params(), rid)
            deadline = time.monotonic() + 10.0
            while True:
                if self._stop.is_set():
                    return
                if time.monotonic() > deadline:
                    raise WebSocketError("認証の応答がない")
                raw = ws.recv_message()
                if raw is None:
                    continue
                data = json.loads(raw)
                if isinstance(data, dict) and data.get("id") == rid:
                    if data.get("result") is True:
                        break
                    raise WebSocketError(f"認証に失敗: {data.get('error')}")
        pending = {}
        for ch in self.channels:
            rid += 1
            pending[rid] = ch
            self._send_rpc(ws, "subscribe", {"channel": ch}, rid)
        self.connected.set()
        log.info(f"[{self.name}] 接続しました（{len(self.channels)} チャンネル）")
        if self.on_connect is not None:
            self.on_connect()
        last_ping = time.monotonic()
        while not self._stop.is_set():
            raw = ws.recv_message()
            if raw is not None:
                recv_ts = time.time()
                data = json.loads(raw)
                items = data if isinstance(data, list) else [data]
                for d in items:
                    if not isinstance(d, dict):
                        continue
                    if d.get("method") == "channelMessage":
                        p = d.get("params") or {}
                        self.msg_count += 1
                        try:
                            self.on_message(p.get("channel"), p.get("message"), recv_ts)
                        except Exception:
                            self.callback_errors += 1
                            if self.callback_errors <= 20 or self.callback_errors % 1000 == 0:
                                log.exception(f"[{self.name}] メッセージ処理でエラー")
                    elif "id" in d:
                        ch = pending.pop(d.get("id"), None)
                        if ch is not None:
                            if d.get("result") is True:
                                self.subscribed.add(ch)
                            else:
                                log.error(f"[{self.name}] 購読に失敗 {ch}: {d.get('error')}")
            now = time.monotonic()
            if now - last_ping >= self.ping_interval:
                ws.ping(b"hb")
                last_ping = now
            if now - ws.last_recv > self.stale_sec:
                raise WebSocketError(f"{self.stale_sec:.0f}秒間なにも届かない")
