"""本机 stub HTTP server：离线验证 OpenRouter transport（不访问公网）。

只用于测试：
- 可脚本化返回 status / body / delay；
- 记录收到的请求（raw body、parsed body、headers），供断言请求契约；
- 使用 `ThreadingHTTPServer`，因此 delay 不会阻塞其它请求。

注意：本模块记录请求 header（包含测试用假 Key），但只存在于内存，绝不落盘。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType

#: 测试用假 Key（真实 Key 绝不进入测试 fixture）。
FAKE_API_KEY = "sk-or-v1-test-only-NOT-A-REAL-KEY"

#: 测试用假 Key 中用于「不得泄漏」检查的标记子串。
FAKE_KEY_MARKER = "NOT-A-REAL-KEY"


@dataclass
class StubResponse:
    """stub 的响应脚本。"""

    status: int = 200
    body: str = ""
    content_type: str = "application/json"
    delay_s: float = 0.0


@dataclass
class RecordedRequest:
    raw_body: str
    body: dict[str, object] | None
    authorization: str | None
    content_type: str | None
    method: str
    path: str


@dataclass
class StubOpenRouterServer:
    """OpenRouter 的本机替身。"""

    response: StubResponse = field(default_factory=StubResponse)
    requests: list[RecordedRequest] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> StubOpenRouterServer:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self))
        self._server = server
        # poll_interval 默认 0.5s，会让 shutdown() 等待半个轮询周期；测试里缩短它。
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self._thread = thread
        return self

    def stop(self) -> None:
        server = self._server
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None

    @property
    def port(self) -> int:
        assert self._server is not None
        return int(self._server.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/api"

    @property
    def endpoint(self) -> str:
        """Chat Completions 路径（P0001.4.1 已废弃的实验路径）。"""
        return f"{self.base_url}/v1/chat/completions"

    @property
    def systemone_endpoint(self) -> str:
        """System One typed 路径（P0001.4.2 热路径）。"""
        return f"{self.base_url}/v1/systemone"

    def set_response(self, response: StubResponse) -> None:
        self.response = response

    def clear_requests(self) -> None:
        self.requests.clear()

    def __enter__(self) -> StubOpenRouterServer:
        return self.start()

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None) -> None:
        self.stop()


def choice_answer(
    *,
    choice: str = "flat",
    probabilities: dict[str, float] | None = None,
    confidence: float | None = 0.42,
) -> dict[str, object]:
    """System One Choice 答案。"""
    from prediction.types import FUTURE_RETURN_CATEGORIES

    if probabilities is None:
        probabilities = {
            "strong_down": 0.05,
            "down": 0.15,
            "flat": 0.6,
            "up": 0.15,
            "strong_up": 0.05,
        }
    assert set(probabilities) >= set(FUTURE_RETURN_CATEGORIES)
    answer: dict[str, object] = {"type": "choice", "choice": choice, "probabilities": probabilities}
    if confidence is not None:
        answer["confidence"] = confidence
    return answer


def noul_answer(probability: float = 0.73) -> dict[str, object]:
    """System One Noul 答案（协议上没有 confidence）。"""
    return {"type": "noul", "noul": probability}


def systemone_answers(
    *,
    horizons_ms: tuple[int, ...] | None = None,
    choice_probabilities: dict[str, float] | None = None,
    confidence: float | None = 0.42,
    noul_probability: float = 0.73,
    overrides: dict[str, object] | None = None,
) -> dict[str, object]:
    """构造一套完整的 4 Choice + 4 Noul typed answers。"""
    from prediction.schema.market_v1 import FUTURE_RETURN_HORIZONS_MS
    from prediction.systemone_wire import BINARY_QUESTION_WIRE_IDS, future_return_question_id

    horizons = horizons_ms if horizons_ms is not None else FUTURE_RETURN_HORIZONS_MS
    answers: dict[str, object] = {
        future_return_question_id(horizon): choice_answer(
            probabilities=choice_probabilities, confidence=confidence
        )
        for horizon in horizons
    }
    for wire_id in BINARY_QUESTION_WIRE_IDS.values():
        answers[wire_id] = noul_answer(noul_probability)
    if overrides:
        answers.update(overrides)
    return answers


def systemone_envelope(
    answers: dict[str, object] | None = None,
    *,
    model: str = "typesafe/jev-1.13-20260917",
    provider: str = "TypeSafe",
    response_id: str = "gen-dec-test-0001",
    usage: dict[str, object] | None = None,
    extra: dict[str, object] | None = None,
) -> str:
    """构造 System One 响应外层（含 provider 侧证据字段）。"""
    payload: dict[str, object] = {
        "id": response_id,
        "model": model,
        "provider": provider,
        "answers": answers if answers is not None else systemone_answers(),
        "usage": usage if usage is not None else {"input_tokens": 512, "output_tokens": 48, "cost": 1.2e-05},
    }
    if extra:
        payload.update(extra)
    return json.dumps(payload, separators=(",", ":"), allow_nan=False)


def openrouter_envelope(content: str, *, model: str = "typesafe/jev-router") -> str:
    """构造一个符合 OpenRouter 外层的响应体。"""
    return json.dumps(
        {
            "id": "gen-stub",
            "model": model,
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        },
        separators=(",", ":"),
    )


def _make_handler(server: StubOpenRouterServer) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length).decode("utf-8") if length else ""
            try:
                parsed: dict[str, object] | None = json.loads(raw_body)
            except json.JSONDecodeError:
                parsed = None
            server.requests.append(
                RecordedRequest(
                    raw_body=raw_body,
                    body=parsed,
                    authorization=self.headers.get("Authorization"),
                    content_type=self.headers.get("Content-Type"),
                    method="POST",
                    path=self.path,
                )
            )

            response = server.response
            if response.delay_s:
                time.sleep(response.delay_s)
            payload = response.body.encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            """保持测试输出干净。"""

    return _Handler


__all__ = [
    "FAKE_API_KEY",
    "FAKE_KEY_MARKER",
    "RecordedRequest",
    "StubOpenRouterServer",
    "StubResponse",
    "choice_answer",
    "noul_answer",
    "openrouter_envelope",
    "systemone_answers",
    "systemone_envelope",
]
