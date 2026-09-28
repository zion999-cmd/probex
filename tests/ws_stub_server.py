"""本地 WebSocket stub server（P0001.9.1 测试脚手架）。

只监听 `127.0.0.1`，用标准库实现 RFC 6455 握手与服务端帧，因此可以离线验证：

- 真实 socket 上的握手（`Sec-WebSocket-Accept`）；
- 客户端帧必须掩码、服务端帧不得掩码；
- 订阅 ack、业务消息、畸形帧、二进制帧、主动断开、静默（订阅到错误 tier 的表现）。

用法：

```python
server = StubWebSocketServer()
server.start()
... 连接 ...
server.wait_until_connected(2)
server.send("/public", json_text)
server.drop("/market")
server.stop()
```
"""

from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass, field

from connectors.binance.market_data.transport import decode_frame, websocket_accept

_OPCODE_TEXT = 0x1
_OPCODE_BINARY = 0x2
_OPCODE_CLOSE = 0x8
_FIN = 0x80
_LOCALHOST = "127.0.0.1"


@dataclass
class StubClient:
    """服务端侧的一条连接。"""

    connection: socket.socket
    path: str
    key: str
    lock: threading.Lock = field(default_factory=threading.Lock)
    closed: bool = False

    def send_text(self, text: str) -> None:
        self._send_frame(_OPCODE_TEXT, text.encode("utf-8"))

    def send_raw(self, payload: bytes, *, opcode: int = _OPCODE_TEXT) -> None:
        self._send_frame(opcode, payload)

    def send_binary(self, payload: bytes) -> None:
        self._send_frame(_OPCODE_BINARY, payload)

    def send_ping(self, payload: bytes = b"ping") -> None:
        self._send_frame(0x9, payload)

    def close(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
        try:
            self.connection.sendall(bytes([_FIN | _OPCODE_CLOSE]) + b"\x00")
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        finally:
            self.connection.close()

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        with self.lock:
            if self.closed:
                raise RuntimeError("stub client is closed")
            header = bytearray([_FIN | opcode])
            length = len(payload)
            if length < 126:
                header.append(length)
            elif length < 65536:
                header.append(126)
                header.extend(length.to_bytes(2, "big"))
            else:
                header.append(127)
                header.extend(length.to_bytes(8, "big"))
            self.connection.sendall(bytes(header) + payload)


class StubWebSocketServer:
    """按路径（tier）路由的 stub WS 服务端。"""

    def __init__(self) -> None:
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((_LOCALHOST, 0))
        self._server.listen(8)
        self._clients: list[StubClient] = []
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running = False
        self._received: list[tuple[str, str]] = []
        self._ack_subscriptions = True

    # ------------------------------------------------------------------ 地址

    @property
    def port(self) -> int:
        return int(self._server.getsockname()[1])

    @property
    def ws_host(self) -> str:
        return f"ws://{_LOCALHOST}:{self.port}"

    @property
    def connected_count(self) -> int:
        with self._lock:
            return len(self._clients)

    @property
    def received(self) -> tuple[tuple[str, str], ...]:
        """收到的客户端文本消息（path, text）。"""
        with self._lock:
            return tuple(self._received)

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        for client in list(self._clients):
            client.close()
        try:
            self._server.close()
        except OSError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=2)

    def wait_until_connected(self, count: int, *, timeout_s: float = 5.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.connected_count >= count:
                return
            time.sleep(0.01)
        raise TimeoutError(f"expected {count} stub connections, got {self.connected_count}")

    # ------------------------------------------------------------------ 推送

    def send(self, path_prefix: str, text: str) -> int:
        """向所有匹配路径前缀的连接推送一条文本消息，返回接收者数量。"""
        recipients = [client for client in self._snapshot() if client.path.startswith(path_prefix)]
        for client in recipients:
            client.send_text(text)
        return len(recipients)

    def send_ack(self, path_prefix: str, *, request_id: int = 1) -> int:
        return self.send(path_prefix, json.dumps({"result": None, "id": request_id}))

    def drop(self, path_prefix: str) -> int:
        recipients = [client for client in self._snapshot() if client.path.startswith(path_prefix)]
        for client in recipients:
            client.close()
        return len(recipients)

    def _snapshot(self) -> list[StubClient]:
        with self._lock:
            return [client for client in self._clients if not client.closed]

    # ------------------------------------------------------------------ 内部

    def _serve(self) -> None:
        while self._running:
            try:
                connection, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(connection,), daemon=True).start()

    def _handle(self, connection: socket.socket) -> None:
        try:
            client = self._handshake(connection)
        except OSError:
            connection.close()
            return
        with self._lock:
            self._clients.append(client)
        try:
            self._read_loop(client)
        finally:
            client.close()

    def _handshake(self, connection: socket.socket) -> StubClient:
        request = bytearray()
        while b"\r\n\r\n" not in request:
            chunk = connection.recv(4096)
            if not chunk:
                raise OSError("client closed during handshake")
            request.extend(chunk)
        text = bytes(request).decode("latin-1")
        lines = text.split("\r\n")
        target = lines[0].split(" ")[1]
        headers = {}
        for line in lines[1:]:
            name, separator, value = line.partition(":")
            if separator:
                headers[name.strip().lower()] = value.strip()
        key = headers.get("sec-websocket-key", "")
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {websocket_accept(key)}\r\n\r\n"
        )
        connection.sendall(response.encode("ascii"))
        return StubClient(connection=connection, path=target, key=key)

    def _read_loop(self, client: StubClient) -> None:
        buffer = bytearray()
        while self._running and not client.closed:
            frame = decode_frame(buffer)
            if frame is None:
                try:
                    chunk = client.connection.recv(65536)
                except OSError:
                    return
                if not chunk:
                    return
                buffer.extend(chunk)
                continue
            if frame.opcode == _OPCODE_CLOSE:
                return
            if frame.opcode == _OPCODE_TEXT:
                text = frame.payload.decode("utf-8", errors="replace")
                with self._lock:
                    self._received.append((client.path, text))
                if self._ack_subscriptions and '"SUBSCRIBE"' in text:
                    client.send_text(json.dumps({"result": None, "id": 1}))


__all__ = ["StubClient", "StubWebSocketServer"]
