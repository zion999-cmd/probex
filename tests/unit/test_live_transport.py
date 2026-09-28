"""P0001.9.1 单元测试：标准库 WebSocket 传输层。"""

from __future__ import annotations

import unittest

from connectors.binance.market_data.errors import (
    WebSocketHandshakeError,
    WebSocketProtocolError,
    WebSocketTimeout,
)
from connectors.binance.market_data.transport import (
    ReconnectPolicy,
    WebSocketConnection,
    build_handshake_request,
    connect,
    decode_frame,
    encode_close_frame,
    encode_pong_frame,
    encode_text_frame,
    parse_handshake_response,
    websocket_accept,
)
from tests.ws_stub_server import StubWebSocketServer

RFC_KEY = "dGhlIHNhbXBsZSBub25jZQ=="
RFC_ACCEPT = "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


class FakeSocket:
    """可脚本化的 socket（recv 返回预设分片，记录 sendall）。"""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.sent: list[bytes] = []
        self.timeout: float | None = None
        self.closed = False

    def recv(self, size: int) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def settimeout(self, timeout: float | None) -> None:
        self.timeout = timeout

    def close(self) -> None:
        self.closed = True


def _server_frame(payload: bytes, *, opcode: int = 0x1) -> bytes:
    return bytes([0x80 | opcode, len(payload)]) + payload


class HandshakeTest(unittest.TestCase):
    def test_rfc_accept_vector(self) -> None:
        self.assertEqual(websocket_accept(RFC_KEY), RFC_ACCEPT)

    def test_handshake_request_shape(self) -> None:
        request = build_handshake_request(host="example.invalid", path="/market/ws/btcusdt@aggTrade", key=RFC_KEY)

        self.assertTrue(request.startswith(b"GET /market/ws/btcusdt@aggTrade HTTP/1.1\r\n"))
        self.assertIn(b"Upgrade: websocket\r\n", request)
        self.assertIn(f"Sec-WebSocket-Accept: ".encode() if False else f"Sec-WebSocket-Key: {RFC_KEY}".encode(), request)

    def test_response_is_validated(self) -> None:
        good = (
            f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Accept: {RFC_ACCEPT}\r\n\r\n"
        ).encode("ascii")

        parse_handshake_response(good, key=RFC_KEY)  # 不抛异常

        with self.assertRaises(WebSocketHandshakeError):
            parse_handshake_response(good.replace(b"101", b"200"), key=RFC_KEY)
        with self.assertRaises(WebSocketHandshakeError):
            parse_handshake_response(good.replace(RFC_ACCEPT.encode(), b"wrong"), key=RFC_KEY)
        with self.assertRaises(WebSocketHandshakeError):
            parse_handshake_response(good.replace(b"websocket", b"other"), key=RFC_KEY)


class FrameTest(unittest.TestCase):
    def test_client_frames_are_masked(self) -> None:
        frame = encode_text_frame("hi", mask_key=b"\x01\x02\x03\x04")

        self.assertEqual(frame[0], 0x81)
        self.assertTrue(frame[1] & 0x80)  # MASK 位
        self.assertEqual(frame[2:6], b"\x01\x02\x03\x04")

    def test_roundtrip_decode(self) -> None:
        buffer = bytearray(encode_text_frame("hello", mask_key=b"\x0a\x0b\x0c\x0d"))

        frame = decode_frame(buffer)

        self.assertIsNotNone(frame)
        self.assertEqual(frame.opcode, 0x1)
        self.assertEqual(frame.payload, b"hello")
        self.assertEqual(len(buffer), 0)

    def test_decode_returns_none_when_incomplete(self) -> None:
        buffer = bytearray(encode_text_frame("hello", mask_key=b"\x0a\x0b\x0c\x0d")[:4])

        self.assertIsNone(decode_frame(buffer))
        self.assertEqual(len(buffer), 4)  # 不消费

    def test_extended_lengths(self) -> None:
        payload = "x" * 200
        frame = decode_frame(bytearray(encode_text_frame(payload, mask_key=b"\x01\x02\x03\x04")))

        self.assertEqual(frame.payload.decode(), payload)

    def test_oversized_frame_is_rejected(self) -> None:
        buffer = bytearray([0x81, 127]) + (20 * 1024 * 1024).to_bytes(8, "big")

        with self.assertRaises(WebSocketProtocolError):
            decode_frame(buffer)

    def test_reconnect_policy_backoff(self) -> None:
        policy = ReconnectPolicy(max_attempts=5, base_backoff_ms=100, max_backoff_ms=400)

        self.assertEqual([policy.delay_ms(n) for n in (1, 2, 3, 4)], [100, 200, 400, 400])
        with self.assertRaises(ValueError):
            policy.delay_ms(0)


class ConnectionTest(unittest.TestCase):
    def _connection(self, chunks: list[bytes]) -> tuple[WebSocketConnection, FakeSocket]:
        sock = FakeSocket(chunks)
        return WebSocketConnection(sock, host="h", path="/p"), sock

    def test_text_and_ping_and_close(self) -> None:
        conn, sock = self._connection([_server_frame(b"a"), _server_frame(b"p", opcode=0x9), _server_frame(b"", opcode=0x8)])

        self.assertEqual(conn.recv_text(timeout_s=1), "a")
        # ping 在内部被应答（不当作业务消息），因此下一条可见消息就是 close → None
        self.assertIsNone(conn.recv_text(timeout_s=1))
        self.assertTrue(conn.closed)
        self.assertTrue(any(frame[0] == 0x8A for frame in sock.sent))  # pong 已发出

    def test_fragmented_message_is_assembled(self) -> None:
        first = bytes([0x01, 3]) + b"abc"  # FIN=0
        second = bytes([0x80, 3]) + b"def"
        conn, _ = self._connection([first + second])

        self.assertEqual(conn.recv_text(timeout_s=1), "abcdef")

    def test_binary_frame_is_rejected(self) -> None:
        conn, _ = self._connection([_server_frame(b"\x00\x01", opcode=0x2)])

        with self.assertRaises(WebSocketProtocolError):
            conn.recv_text(timeout_s=1)

    def test_empty_read_means_closed(self) -> None:
        conn, _ = self._connection([])

        with self.assertRaises(Exception):
            conn.recv_text(timeout_s=1)

    def test_payload_after_handshake_is_preserved(self) -> None:
        sock = FakeSocket([])
        conn = WebSocketConnection(sock, host="h", path="/p", initial_bytes=_server_frame(b"early"))

        self.assertEqual(conn.recv_text(timeout_s=1), "early")

    def test_close_sends_close_frame_once(self) -> None:
        conn, sock = self._connection([])

        conn.close()
        conn.close()

        self.assertTrue(sock.closed)
        self.assertEqual(len([frame for frame in sock.sent if frame[0] == 0x88]), 1)

    def test_ping_and_pong_frames_are_counted(self) -> None:
        """心跳帧计数：user data stream 的健康证据（pong 由客户端自动应答）。"""
        conn, sock = self._connection(
            [_server_frame(b"p1", opcode=0x9), _server_frame(b"pong", opcode=0xA), _server_frame(b"x")]
        )

        self.assertEqual(conn.recv_text(timeout_s=1), "x")  # ping/pong 被内部消费
        self.assertEqual(conn.ping_count, 1)
        self.assertEqual(conn.pong_count, 1)
        self.assertTrue(any(frame[0] == 0x8A for frame in sock.sent))  # 已应答

    def test_pong_and_close_encoders(self) -> None:
        self.assertEqual(encode_pong_frame(b"x", mask_key=b"\x01\x02\x03\x04")[0], 0x8A)
        self.assertEqual(encode_close_frame(mask_key=b"\x01\x02\x03\x04")[0], 0x88)


class RealSocketTest(unittest.TestCase):
    """真实 socket 上的握手与收发（本地 stub server，无外部网络）。"""

    def setUp(self) -> None:
        self.server = StubWebSocketServer()
        self.server.start()
        self.addCleanup(self.server.stop)

    def test_connect_subscribe_and_receive(self) -> None:
        ws = connect(f"{self.server.ws_host}/public/stream?streams=btcusdt@depth@100ms", timeout_s=5)
        self.addCleanup(ws.close)
        self.server.wait_until_connected(1)

        ws.send_text('{"method":"SUBSCRIBE","params":["btcusdt@depth@100ms"],"id":1}')
        self.assertEqual(ws.recv_text(timeout_s=5) is not None, True)
        self.assertIn("/public/", ws.path)

        self.server.send("/public", '{"data":{"e":"depthUpdate"}}')
        message = ws.recv_text(timeout_s=5)
        self.assertIn("depthUpdate", message or "")

    def test_peer_drop_returns_none(self) -> None:
        ws = connect(f"{self.server.ws_host}/market/stream?streams=btcusdt@aggTrade", timeout_s=5)
        self.addCleanup(ws.close)
        self.server.wait_until_connected(1)

        self.server.drop("/market")

        self.assertIsNone(ws.recv_text(timeout_s=5))

    def test_silent_server_times_out(self) -> None:
        ws = connect(f"{self.server.ws_host}/market/stream?streams=btcusdt@markPrice@1s", timeout_s=5)
        self.addCleanup(ws.close)
        self.server.wait_until_connected(1)

        with self.assertRaises(WebSocketTimeout):
            ws.recv_text(timeout_s=0.2)

    def test_connection_to_closed_port_fails_visibly(self) -> None:
        self.server.stop()

        with self.assertRaises(Exception):
            connect(f"{self.server.ws_host}/public/ws", timeout_s=0.5)


if __name__ == "__main__":
    unittest.main()
