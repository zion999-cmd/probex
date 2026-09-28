"""WebSocket 传输层：标准库手写实现（P0001.9.1 §0.1）。

为什么手写：仓库零第三方依赖（D-002 / §16），本提案未授权新增依赖。

职责边界（**wall-clock 只允许出现在这里**）：

- TCP + TLS 连接、HTTP Upgrade 握手、`Sec-WebSocket-Accept` 校验；
- 文本消息收发、掩码规则、ping/pong 自动应答、close 处理、分片重组；
- 超时错误上抛（`WebSocketTimeout`），由调用方决定重连或继续等待。

不做的事：不做静默 fallback / 自动换 tier（未授权的错误恢复语义）；不主动发业务订阅
（订阅报文由 `streams.subscribe_message` 生成，调用方发送）。
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from connectors.binance.market_data.errors import (
    TransportError,
    WebSocketClosed,
    WebSocketHandshakeError,
    WebSocketProtocolError,
    WebSocketTimeout,
)

#: RFC 6455 握手用的固定 GUID。
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

_OPCODE_CONTINUATION = 0x0
_OPCODE_TEXT = 0x1
_OPCODE_BINARY = 0x2
_OPCODE_CLOSE = 0x8
_OPCODE_PING = 0x9
_OPCODE_PONG = 0xA
_FIN = 0x80
_MASK = 0x80

#: 单次 `recv` 读取上限。
READ_CHUNK = 65536
#: 允许的最大消息长度（防止对端用超大帧打爆内存）。
MAX_MESSAGE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ReconnectPolicy:
    """重连策略（显式注入，无默认值、无随机抖动，保证可复现）。"""

    #: 单次断开后最多尝试几次（0 表示不重连，直接上抛）。
    max_attempts: int
    base_backoff_ms: int
    max_backoff_ms: int

    def __post_init__(self) -> None:
        for field in ("max_attempts", "base_backoff_ms", "max_backoff_ms"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"ReconnectPolicy.{field} must be an int >= 0, got {value!r}")
        if self.max_backoff_ms < self.base_backoff_ms:
            raise ValueError("ReconnectPolicy.max_backoff_ms must be >= base_backoff_ms")

    def delay_ms(self, attempt: int) -> int:
        """第 `attempt` 次尝试前的等待时间（指数退避，封顶）。"""
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ValueError(f"attempt must be an int >= 1, got {attempt!r}")
        return min(self.max_backoff_ms, self.base_backoff_ms * (2 ** (attempt - 1)))


class SocketLike(Protocol):
    """`socket.socket` 的最小结构契约（便于注入假 socket 做离线测试）。"""

    def recv(self, size: int) -> bytes: ...

    def sendall(self, data: bytes) -> None: ...

    def settimeout(self, timeout: float | None) -> None: ...

    def close(self) -> None: ...


def websocket_accept(key: str) -> str:
    """由 `Sec-WebSocket-Key` 计算期望的 `Sec-WebSocket-Accept`。"""
    digest = hashlib.sha1(f"{key}{WS_GUID}".encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def build_handshake_request(*, host: str, path: str, key: str) -> bytes:
    """构造客户端握手请求（HTTP/1.1 Upgrade）。"""
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {host}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
        "",
        "",
    ]
    return "\r\n".join(lines).encode("ascii")


def parse_handshake_response(raw: bytes, *, key: str) -> None:
    """校验握手响应；失败抛 `WebSocketHandshakeError`。"""
    text = raw.decode("latin-1", errors="replace")
    head, _, _ = text.partition("\r\n\r\n")
    lines = head.split("\r\n")
    if not lines or " 101" not in lines[0]:
        raise WebSocketHandshakeError(f"unexpected handshake status line: {lines[0] if lines else ''!r}")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, separator, value = line.partition(":")
        if separator:
            headers[name.strip().lower()] = value.strip()
    if headers.get("upgrade", "").lower() != "websocket":
        raise WebSocketHandshakeError("handshake response is missing 'Upgrade: websocket'")
    expected = websocket_accept(key)
    if headers.get("sec-websocket-accept") != expected:
        raise WebSocketHandshakeError("handshake response has an invalid Sec-WebSocket-Accept")
    if "sec-websocket-extensions" in headers:
        raise WebSocketHandshakeError("server negotiated an extension we did not offer")


def encode_text_frame(payload: str, *, mask_key: bytes) -> bytes:
    """编码一个客户端文本帧（客户端必须掩码）。"""
    if len(mask_key) != 4:
        raise ValueError("mask_key must be 4 bytes")
    data = payload.encode("utf-8")
    header = bytearray([_FIN | _OPCODE_TEXT])
    header.extend(_encode_length(len(data), masked=True))
    header.extend(mask_key)
    return bytes(header) + _apply_mask(data, mask_key)


def encode_pong_frame(payload: bytes, *, mask_key: bytes) -> bytes:
    header = bytearray([_FIN | _OPCODE_PONG])
    header.extend(_encode_length(len(payload), masked=True))
    header.extend(mask_key)
    return bytes(header) + _apply_mask(payload, mask_key)


def encode_close_frame(*, mask_key: bytes, code: int = 1000) -> bytes:
    payload = code.to_bytes(2, "big")
    header = bytearray([_FIN | _OPCODE_CLOSE])
    header.extend(_encode_length(len(payload), masked=True))
    header.extend(mask_key)
    return bytes(header) + _apply_mask(payload, mask_key)


@dataclass(frozen=True, slots=True)
class DecodedFrame:
    """一帧的解析结果。"""

    fin: bool
    opcode: int
    payload: bytes
    size: int


def decode_frame(buffer: bytearray) -> DecodedFrame | None:
    """从缓冲区尝试解析一帧；数据不足返回 `None`（不消费缓冲区）。"""
    if len(buffer) < 2:
        return None
    first, second = buffer[0], buffer[1]
    fin = bool(first & _FIN)
    opcode = first & 0x0F
    masked = bool(second & _MASK)
    length = second & 0x7F
    offset = 2
    if length == 126:
        if len(buffer) < offset + 2:
            return None
        length = int.from_bytes(buffer[offset : offset + 2], "big")
        offset += 2
    elif length == 127:
        if len(buffer) < offset + 8:
            return None
        length = int.from_bytes(buffer[offset : offset + 8], "big")
        offset += 8
    if length > MAX_MESSAGE_BYTES:
        raise WebSocketProtocolError(f"frame payload {length} exceeds the {MAX_MESSAGE_BYTES} byte limit")
    if masked:
        if len(buffer) < offset + 4:
            return None
        mask_key = bytes(buffer[offset : offset + 4])
        offset += 4
    else:
        mask_key = b""
    if len(buffer) < offset + length:
        return None
    payload = bytes(buffer[offset : offset + length])
    del buffer[: offset + length]
    if masked:
        payload = _apply_mask(payload, mask_key)
    return DecodedFrame(fin=fin, opcode=opcode, payload=payload, size=offset + length)


class WebSocketConnection:
    """一条已握手的 WS 连接（同步阻塞 + 超时）。"""

    def __init__(self, sock: SocketLike, *, host: str, path: str, initial_bytes: bytes = b"") -> None:
        self._socket = sock
        self._host = host
        self._path = path
        # 握手响应之后可能已经在同一个 TCP 段里带了首批 WS 帧，必须保留
        self._buffer = bytearray(initial_bytes)
        self._closed = False
        self._closed_explicitly = False

    @property
    def host(self) -> str:
        return self._host

    @property
    def path(self) -> str:
        return self._path

    @property
    def closed(self) -> bool:
        """对端是否已关闭连接（收到 close 帧或 socket 返回空）。"""
        return self._closed

    def send_text(self, text: str) -> None:
        if self._closed:
            raise WebSocketClosed("connection is closed")
        try:
            self._socket.sendall(encode_text_frame(text, mask_key=_new_mask_key()))
        except OSError as exc:
            raise TransportError(f"send failed: {exc}") from exc

    def recv_text(self, *, timeout_s: float) -> str | None:
        """读取一条完整文本消息；对端关闭返回 `None`；超时抛 `WebSocketTimeout`。"""
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or timeout_s <= 0:
            raise ValueError(f"timeout_s must be a positive number, got {timeout_s!r}")
        if self._closed:
            return None
        self._socket.settimeout(float(timeout_s))
        message = bytearray()
        while True:
            closed, text = self._handle_frame(self._take_frame(), message)
            if closed:
                return None
            if text is not None:
                return text

    def close(self) -> None:
        """发送 close 帧并关闭 socket（幂等）。"""
        if self._closed_explicitly:
            return
        self._closed_explicitly = True
        try:
            if not self._closed:
                self._socket.sendall(encode_close_frame(mask_key=_new_mask_key()))
        except OSError:
            pass
        finally:
            self._closed = True
            self._socket.close()

    # ------------------------------------------------------------------ 内部

    def _take_frame(self) -> DecodedFrame | None:
        while True:
            frame = decode_frame(self._buffer)
            if frame is not None:
                return frame
            self._read_more()

    def _read_more(self) -> None:
        try:
            chunk = self._socket.recv(READ_CHUNK)
        except socket.timeout:
            raise WebSocketTimeout("no complete message within the timeout") from None
        except OSError as exc:
            raise TransportError(f"recv failed: {exc}") from exc
        if not chunk:
            self._closed = True
            raise WebSocketClosed("peer closed the connection")
        self._buffer.extend(chunk)

    def _handle_frame(self, frame: DecodedFrame, message: bytearray) -> tuple[bool, str | None]:
        """处理一帧：返回 `(对端是否已关闭, 完整文本消息)`。"""
        if frame.opcode == _OPCODE_CLOSE:
            self._closed = True
            return (True, None)
        if frame.opcode == _OPCODE_PING:
            self._socket.sendall(encode_pong_frame(frame.payload, mask_key=_new_mask_key()))
            return (False, None)
        if frame.opcode == _OPCODE_PONG:
            return (False, None)
        if frame.opcode == _OPCODE_BINARY:
            raise WebSocketProtocolError("unexpected binary frame (Binance streams are text)")
        if frame.opcode == _OPCODE_CONTINUATION:
            message.extend(frame.payload)
        elif frame.opcode == _OPCODE_TEXT:
            message.clear()
            message.extend(frame.payload)
        else:
            raise WebSocketProtocolError(f"unsupported opcode {frame.opcode}")
        if len(message) > MAX_MESSAGE_BYTES:
            raise WebSocketProtocolError("assembled message exceeds the limit")
        if not frame.fin:
            return (False, None)
        return (False, message.decode("utf-8", errors="strict"))


def connect(
    url: str,
    *,
    timeout_s: float,
    socket_factory: object = None,
    ssl_context: ssl.SSLContext | None = None,
) -> WebSocketConnection:
    """建立 WS 连接并完成握手。

    `timeout_s` 同时用于 TCP 连接与握手读取；`socket_factory` 可注入（测试用），
    默认使用 `socket.create_connection`。
    """
    if not isinstance(url, str) or not url.startswith(("ws://", "wss://")):
        raise TransportError(f"url must start with ws:// or wss://, got {url!r}")
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or timeout_s <= 0:
        raise TransportError(f"timeout_s must be a positive number, got {timeout_s!r}")
    parts = urlsplit(url)
    host = parts.hostname
    if host is None:
        raise TransportError(f"url has no host: {url!r}")
    port = parts.port if parts.port is not None else (443 if parts.scheme == "wss" else 80)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    sock = _open_socket(
        host=host, port=port, scheme=parts.scheme, timeout_s=float(timeout_s),
        socket_factory=socket_factory, ssl_context=ssl_context,
    )
    return _perform_handshake(sock, host=host, path=path, timeout_s=float(timeout_s))


def _open_socket(
    *,
    host: str,
    port: int,
    scheme: str,
    timeout_s: float,
    socket_factory: object,
    ssl_context: ssl.SSLContext | None,
) -> SocketLike:
    try:
        if socket_factory is not None:
            raw = socket_factory(host, port, timeout_s)  # type: ignore[operator]
        else:
            raw = socket.create_connection((host, port), timeout=timeout_s)
    except OSError as exc:
        raise TransportError(f"cannot connect to {host}:{port}: {exc}") from exc
    if scheme == "wss":
        context = ssl_context if ssl_context is not None else ssl.create_default_context()
        try:
            return context.wrap_socket(raw, server_hostname=host)
        except ssl.SSLError as exc:
            raw.close()
            raise TransportError(f"TLS handshake failed for {host}: {exc}") from exc
    return raw


def _perform_handshake(sock: SocketLike, *, host: str, path: str, timeout_s: float) -> WebSocketConnection:
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    sock.settimeout(timeout_s)
    try:
        sock.sendall(build_handshake_request(host=host, path=path, key=key))
        head, rest = _read_handshake_response(sock)
    except (OSError, TransportError) as exc:
        sock.close()
        if isinstance(exc, TransportError):
            raise
        raise TransportError(f"handshake I/O failed: {exc}") from exc
    try:
        parse_handshake_response(head, key=key)
    except WebSocketHandshakeError:
        sock.close()
        raise
    return WebSocketConnection(sock, host=host, path=path, initial_bytes=rest)


def _read_handshake_response(sock: SocketLike) -> tuple[bytes, bytes]:
    """读到 `\\r\\n\\r\\n` 为止，返回 `(响应头部, 之后的多余字节)`。

    多余字节通常就是首批 WS 帧（服务端可能在同一 TCP 段里连响应一起发出），
    必须交回连接缓冲区，不能丢弃。
    """
    buffer = bytearray()
    while b"\r\n\r\n" not in buffer:
        chunk = sock.recv(READ_CHUNK)
        if not chunk:
            raise TransportError("connection closed during WS handshake")
        buffer.extend(chunk)
        if len(buffer) > 16384:
            raise TransportError("WS handshake response is unexpectedly large")
    head, _, rest = bytes(buffer).partition(b"\r\n\r\n")
    return head + b"\r\n\r\n", rest


def _encode_length(length: int, *, masked: bool) -> bytes:
    flag = _MASK if masked else 0
    if length < 126:
        return bytes([flag | length])
    if length < 65536:
        return bytes([flag | 126]) + length.to_bytes(2, "big")
    return bytes([flag | 127]) + length.to_bytes(8, "big")


def _apply_mask(data: bytes, mask_key: bytes) -> bytes:
    return bytes(byte ^ mask_key[index % 4] for index, byte in enumerate(data))


def _new_mask_key() -> bytes:
    return os.urandom(4)


__all__ = [
    "MAX_MESSAGE_BYTES",
    "ReconnectPolicy",
    "READ_CHUNK",
    "WS_GUID",
    "DecodedFrame",
    "SocketLike",
    "WebSocketConnection",
    "build_handshake_request",
    "connect",
    "decode_frame",
    "encode_close_frame",
    "encode_pong_frame",
    "encode_text_frame",
    "parse_handshake_response",
    "websocket_accept",
]
