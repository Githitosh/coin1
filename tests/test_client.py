"""自作クライアント（REST / WebSocket / JSON-RPC）のテスト"""

import hashlib
import json
import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bfbot.rest import APIError, BitflyerREST, LocalRateLimit, SlidingWindowLimiter, UncertainOrderError  # noqa: E402
from bfbot.ws import (OP_CONT, OP_PING, OP_PONG, OP_TEXT, OP_CLOSE, RealtimeClient, WebSocket,  # noqa: E402
                      WebSocketClosed, encode_frame, try_decode_frame, xor_mask)
from tests.fake_exchange import KEY, SECRET, FakeExchange, WSConn  # noqa: E402


class TestFrames(unittest.TestCase):
    def test_roundtrip_lengths(self):
        for n in (0, 1, 125, 126, 1000, 65535, 65536, 200000):
            data = os.urandom(n)
            for mask in (True, False):
                fr = encode_frame(OP_TEXT, data, mask=mask)
                fin, op, payload, used = try_decode_frame(bytearray(fr))
                self.assertTrue(fin)
                self.assertEqual(op, OP_TEXT)
                self.assertEqual(payload, data)
                self.assertEqual(used, len(fr))

    def test_partial_frame_not_consumed(self):
        fr = encode_frame(OP_TEXT, b"x" * 300, mask=False)
        for cut in range(len(fr)):
            self.assertIsNone(try_decode_frame(bytearray(fr[:cut])))

    def test_mask_rfc_example(self):
        # RFC 6455 5.7 の例: "Hello" をマスク 37 fa 21 3d
        self.assertEqual(xor_mask(b"Hello", bytes.fromhex("37fa213d")), bytes.fromhex("7f9f4d5158"))


class MiniServer(threading.Thread):
    """ハンドシェイク応答と同じパケットでフレームを送る・分割・ping・大きいフレーム・close を試す"""

    def __init__(self):
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.client_frames = []
        self.unmasked = 0
        self.big = json.dumps({"big": "x" * 70000}).encode()

    def run(self):
        c, _ = self.sock.accept()
        conn = WSConn(c)
        data = b""
        while b"\r\n\r\n" not in data:
            data += c.recv(4096)
        import base64
        key = [ln.split(":", 1)[1].strip() for ln in data.decode().split("\r\n")
               if ln.lower().startswith("sec-websocket-key")][0]
        from bfbot.ws import GUID
        acc = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        resp = (f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {acc}\r\n\r\n").encode()
        c.sendall(resp + encode_frame(OP_TEXT, b'{"hello":1}', mask=False))
        c.sendall(encode_frame(OP_TEXT, b'{"part":', mask=False, fin=False)
                  + encode_frame(OP_CONT, b'"ab"}', mask=False, fin=True))
        c.sendall(encode_frame(OP_PING, b"pp", mask=False))
        # 大きいフレームを少しずつ送る（受信側のタイムアウトで同期がずれないか）
        fr = encode_frame(OP_TEXT, self.big, mask=False)
        for i in range(0, len(fr), 7000):
            c.sendall(fr[i:i + 7000])
            time.sleep(0.01)
        conn.sock.settimeout(0.2)
        deadline = time.time() + 3
        while time.time() < deadline and len(self.client_frames) < 2:
            for op, payload in conn.recv_frames():
                self.client_frames.append((op, payload))
        self.unmasked = conn.unmasked_from_client
        c.sendall(encode_frame(OP_CLOSE, b"\x03\xe8bye", mask=False))
        time.sleep(0.2)
        c.close()


class TestWebSocket(unittest.TestCase):
    def test_client_against_local_server(self):
        srv = MiniServer()
        srv.start()
        ws = WebSocket(f"ws://127.0.0.1:{srv.port}/json-rpc", read_timeout=0.005)
        ws.connect()
        got = []
        deadline = time.time() + 5
        while len(got) < 3 and time.time() < deadline:
            m = ws.recv_message()
            if m is not None:
                got.append(m)
        self.assertEqual(got[0], '{"hello":1}')
        self.assertEqual(got[1], '{"part":"ab"}')
        self.assertEqual(json.loads(got[2])["big"], "x" * 70000)
        ws.send_text("client-msg")
        with self.assertRaises(WebSocketClosed):
            deadline = time.time() + 5
            while time.time() < deadline:
                ws.recv_message()
        srv.join(5)
        ops = [op for op, _ in srv.client_frames]
        self.assertIn(OP_PONG, ops)               # ping に pong を返した
        self.assertIn((OP_TEXT, b"client-msg"), srv.client_frames)
        self.assertEqual(srv.unmasked, 0)         # クライアントのフレームは全部マスク済み


class TestRealtimeAndRest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ex = FakeExchange().start()

    @classmethod
    def tearDownClass(cls):
        cls.ex.stop()

    def test_public_and_private_channels(self):
        got = []
        ev = threading.Event()

        def on_msg(ch, m, t):
            got.append(ch)
            if len(got) > 20:
                ev.set()

        pub = RealtimeClient(["lightning_board_snapshot_FX_BTC_JPY", "lightning_board_FX_BTC_JPY",
                              "lightning_executions_FX_BTC_JPY"], on_msg, url=self.ex.ws_url, name="t-pub")
        pub.start()
        self.assertTrue(ev.wait(5))
        self.assertTrue(pub.connected.is_set())
        self.assertEqual(len(pub.subscribed), 3)
        pub.stop()

        priv_events = []
        priv = RealtimeClient(["child_order_events"], lambda ch, m, t: priv_events.append(m),
                              url=self.ex.ws_url, api_key=KEY, api_secret=SECRET, name="t-priv")
        priv.start()
        self.assertTrue(priv.connected.wait(5))
        deadline = time.time() + 3
        while "child_order_events" not in priv.subscribed and time.time() < deadline:
            time.sleep(0.05)
        self.ex.push_events([{"product_code": "FX_BTC_JPY", "event_type": "ORDER"}])
        deadline = time.time() + 3
        while not priv_events and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(priv_events[0][0]["event_type"], "ORDER")
        priv.stop()

    def test_wrong_secret_auth_fails(self):
        c = RealtimeClient(["child_order_events"], lambda *a: None, url=self.ex.ws_url, api_key=KEY,
                           api_secret="wrong", name="t-bad", min_backoff=30)
        c.start()
        time.sleep(1.0)
        self.assertFalse(c.connected.is_set())
        self.assertIn("認証", c.last_error)
        c.stop()

    def test_rest_signature_query_keepalive(self):
        rest = BitflyerREST(KEY, SECRET, base_url=self.ex.rest_base)
        before = self.ex.good_sigs
        self.assertEqual(rest.get_boardstate("FX_BTC_JPY")["state"], "RUNNING")
        self.assertEqual(rest.get_positions("FX_BTC_JPY"), [])          # クエリ付き GET の署名
        self.assertIn("collateral", rest.get_collateral())
        self.assertEqual(self.ex.good_sigs - before, 2)
        self.assertEqual(rest.connections_opened, 1)                     # 接続を使い回している
        bad = BitflyerREST(KEY, "wrong", base_url=self.ex.rest_base)
        with self.assertRaises(APIError) as cm:
            bad.get_collateral()
        self.assertIn("-500", str(cm.exception))

    def test_order_payload(self):
        rest = BitflyerREST(KEY, SECRET, base_url=self.ex.rest_base)
        res = rest.send_child_order("FX_BTC_JPY", "BUY", 0.001, "LIMIT", price=15000000.0,
                                    time_in_force="IOC", minute_to_expire=1)
        self.assertTrue(res["child_order_acceptance_id"].startswith("JRF"))
        body = self.ex.order_requests[-1]
        self.assertEqual(body, {"product_code": "FX_BTC_JPY", "child_order_type": "LIMIT", "side": "BUY",
                                "size": 0.001, "price": 15000000, "minute_to_expire": 1,
                                "time_in_force": "IOC"})


class DropServer(threading.Thread):
    """リクエストを読んだら何も返さず切る（発注の結果が分からない状況）"""

    def __init__(self):
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.requests = 0

    def run(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            c.recv(65536)
            self.requests += 1
            c.close()


class TestRestFailures(unittest.TestCase):
    def test_post_is_not_retried_and_reports_uncertain(self):
        srv = DropServer()
        srv.start()
        rest = BitflyerREST(KEY, SECRET, base_url=f"http://127.0.0.1:{srv.port}", timeout=2)
        with self.assertRaises(UncertainOrderError):
            rest.send_child_order("FX_BTC_JPY", "BUY", 0.001, "LIMIT", price=1)
        self.assertEqual(srv.requests, 1)  # 二重発注しない
        with self.assertRaises(APIError):
            rest.get_ticker("FX_BTC_JPY")
        self.assertEqual(srv.requests, 3)  # GET は1回だけ再試行
        srv.sock.close()

    def test_limiter(self):
        lim = SlidingWindowLimiter(3, 10.0)
        for _ in range(3):
            self.assertEqual(lim.wait_time(0.0), 0.0)
            lim.take(0.0)
        self.assertGreater(lim.wait_time(1.0), 0)
        self.assertEqual(lim.wait_time(10.5), 0.0)
        rest = BitflyerREST(KEY, SECRET, base_url="http://127.0.0.1:9", order_limit=(0, 300))
        with self.assertRaises(LocalRateLimit):
            rest.send_child_order("FX_BTC_JPY", "BUY", 0.001, "LIMIT", price=1)

    def test_error_body_with_negative_status(self):
        with self.assertRaises(APIError):
            BitflyerREST._parse(200, json.dumps({"status": -208, "error_message": "x"}).encode(), "/p")
        self.assertEqual(BitflyerREST._parse(200, b'{"status":"NORMAL"}', "/p"), {"status": "NORMAL"})


if __name__ == "__main__":
    unittest.main()
