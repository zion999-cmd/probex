"""**DEPRECATED（P0001.4.2 / D-017 / D-018）** —— Chat Completions 热路径已被正式废弃。

结论：`OpenRouterTransport` 本身 VALIDATED，但 `typesafe/jev-router` 作为热路径 Jev Provider 为
`REJECTED_FOR_NOW`（model identity 不稳定：实测解析为 `stealth/space-bunny-alpha`；输出契约不稳定；
4.9–19 s latency）。热路径现为 `prediction.providers.systemone.SystemOneProvider`（`POST /api/v1/systemone`）。

本模块**不再从包命名空间导出**，仅为 P0001.4.1 的实验记录而保留；请勿在新的热路径代码中使用。
保留 `DEPRECATED` / `DEPRECATION_REASON` 常量，供结构性测试断言。

原始说明（历史）：
OpenRouter transport：真实 Jev（`typesafe/jev-router`）的 HTTP adapter。

分层（P0001.4.1 确认契约）：

```text
OpenRouterTransport   ← 本模块：HTTP + Authorization + timeout + envelope 提取
     ↓ choices[0].message.content
JevProvider           ← P0001.4 既有 adapter
     ↓ 原始 Jev 内容
Prediction parser     ← P0001.4 既有 strict parser（不改动）
```

职责边界：

- 本模块只负责 HTTP 请求、`Authorization`、timeout、OpenRouter 请求/响应 envelope、
  HTTP status / transport error，以及调用 telemetry（request bytes / status /
  response bytes / latency）。
- 本模块**不负责** probability interpretation、TTL、stale response、backoff policy、
  Strategy、Risk —— 这些属于 P0001.4 的上层。
- 真实 Jev 返回的领域语义（`market_*` / `toxicity_*` / `fill_*`）不由本模块解释，
  也不做模糊兼容：`choices[0].message.content` 原样交给上层 strict parser。

安全：API Key 只从环境变量 `OPENROUTER_API_KEY` 读取，只放进请求头。
它绝不进入 telemetry、异常文本、`PredictionRecord` 或任何落盘文件。

时间：本模块是预测层**唯一**允许使用 wall-clock 的边界（`time.monotonic`），
且仅用于 latency telemetry；其它预测层模块仍不得使用 wall-clock。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from prediction.errors import (
    PredictionError,
    PredictionInvalidResponseError,
    PredictionProviderError,
    PredictionTimeoutError,
    PredictionTransportError,
)
from prediction.types import PredictionRequest

#: 已废弃（P0001.4.2）：Chat Completions 热路径入口。
DEPRECATED = True

#: 废弃原因（供审计与结构性测试）。
DEPRECATION_REASON = (
    "D-017/D-018: typesafe/jev-router 解析为 stealth/space-bunny-alpha（model identity 不稳定）、"
    "输出契约不稳定（自然语言/围栏 JSON/裸 JSON 漂移）、latency 4.9-19s；热路径改为 "
    "SystemOneProvider (POST /api/v1/systemone)"
)

#: 确认的 OpenRouter endpoint（不得自行更换）。
OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

#: 确认的模型标识。
OPENROUTER_MODEL = "typesafe/jev-router"

#: API Key 的环境变量名（Key 不落盘）。
OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"

#: 默认 HTTP timeout（秒）；运行时可覆盖。
DEFAULT_TIMEOUT_S = 30.0

#: 客户端侧错误：我们的请求 / 凭证被拒，provider 没有作答。
_CLIENT_ERROR_STATUSES = frozenset({400, 401, 403, 404, 405, 406, 409, 410, 413, 415, 422})

#: 服务端侧错误：限流 / 服务不可用，应由上层 backoff 处理。
_PROVIDER_ERROR_STATUSES = frozenset({408, 425, 429})


@dataclass(frozen=True, slots=True)
class OpenRouterCall:
    """一次 OpenRouter 调用的 telemetry（不含任何凭证）。"""

    endpoint: str
    model: str
    request_body: str
    http_status: int | None
    response_body: str
    content: str
    latency_ms: int
    error: str | None

    @property
    def request_bytes(self) -> int:
        return len(self.request_body.encode("utf-8"))

    @property
    def response_bytes(self) -> int:
        return len(self.response_body.encode("utf-8"))

    @property
    def content_bytes(self) -> int:
        return len(self.content.encode("utf-8"))

    @property
    def succeeded(self) -> bool:
        return self.error is None

    def as_dict(self) -> dict[str, object]:
        return {
            "endpoint": self.endpoint,
            "model": self.model,
            "request_bytes": self.request_bytes,
            "http_status": self.http_status,
            "response_bytes": self.response_bytes,
            "content_bytes": self.content_bytes,
            "latency_ms": self.latency_ms,
            "error": self.error,
        }


@dataclass
class _HttpAttempt:
    """内部可变载体：让 telemetry 在异常路径上也能拿到 status / body。"""

    status: int | None = None
    body: str = ""


@dataclass
class _CallTelemetry:
    """内部可变载体：收集一次调用的全部 telemetry 字段。"""

    body: str
    started: float
    attempt: _HttpAttempt = field(default_factory=_HttpAttempt)
    content: str = ""
    error: str | None = None


def build_chat_completions_body(request: PredictionRequest, *, model: str = OPENROUTER_MODEL) -> str:
    """按确认契约构造 OpenAI-compatible Chat Completions body。

    canonical Jev payload 作为单条 user message 的 `content`（不改动其结构）。
    """
    if not isinstance(model, str) or not model:
        raise ValueError("model must be a non-empty string")
    body = {
        "model": model,
        "messages": [{"role": "user", "content": request.payload_json}],
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def extract_message_content(response_body: str) -> str:
    """从 OpenRouter envelope 提取 `choices[0].message.content`。"""
    if not isinstance(response_body, str) or not response_body.strip():
        raise PredictionInvalidResponseError("OpenRouter response body is empty")
    try:
        parsed = json.loads(response_body)
    except json.JSONDecodeError as exc:
        raise PredictionInvalidResponseError(f"OpenRouter response body is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise PredictionInvalidResponseError(
            f"OpenRouter envelope must be a JSON object, got {type(parsed).__name__}"
        )

    choices = parsed.get("choices")
    if not isinstance(choices, list) or not choices:
        raise PredictionInvalidResponseError("OpenRouter envelope has no non-empty 'choices' list")

    first = choices[0]
    if not isinstance(first, dict):
        raise PredictionInvalidResponseError("OpenRouter envelope 'choices[0]' must be an object")

    message = first.get("message")
    if not isinstance(message, dict):
        raise PredictionInvalidResponseError("OpenRouter envelope 'choices[0].message' is missing")

    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise PredictionInvalidResponseError("OpenRouter envelope 'choices[0].message.content' is empty")
    return content


def map_http_status(status: int, *, response_excerpt: str = "") -> PredictionError:
    """把非 2xx HTTP 状态映射为既有 failure type（绝不伪造 Prediction）。"""
    detail = f"; body excerpt: {response_excerpt[:500]}" if response_excerpt else ""
    if status in _PROVIDER_ERROR_STATUSES or status >= 500:
        return PredictionProviderError(f"OpenRouter returned HTTP {status}{detail}")
    if status in _CLIENT_ERROR_STATUSES or 400 <= status < 500:
        return PredictionTransportError(f"OpenRouter rejected the request with HTTP {status}{detail}")
    return PredictionTransportError(f"OpenRouter returned unexpected HTTP {status}{detail}")


class OpenRouterTransport:
    """`JevTransport` 实现：调用 OpenRouter chat completions 并返回 Jev content。"""

    def __init__(
        self,
        *,
        model: str = OPENROUTER_MODEL,
        endpoint: str = OPENROUTER_ENDPOINT,
        api_key: str | None = None,
        api_key_env: str = OPENROUTER_API_KEY_ENV,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        if not isinstance(model, str) or not model:
            raise ValueError("model must be a non-empty string")
        if not isinstance(endpoint, str) or not endpoint.startswith(("http://", "https://")):
            raise ValueError("endpoint must be an http(s) URL")
        if not isinstance(api_key_env, str) or not api_key_env:
            raise ValueError("api_key_env must be a non-empty string")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be > 0")
        if api_key is not None and (not isinstance(api_key, str) or not api_key):
            raise ValueError("api_key must be a non-empty string or None")

        self._model = model
        self._endpoint = endpoint
        self._api_key = api_key
        self._api_key_env = api_key_env
        self._timeout_s = timeout_s
        self._calls: list[OpenRouterCall] = []

    @property
    def model(self) -> str:
        return self._model

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def timeout_s(self) -> float:
        return self._timeout_s

    @property
    def calls(self) -> tuple[OpenRouterCall, ...]:
        """调用 telemetry 日志（只含 request/response 与状态，不含凭证）。"""
        return tuple(self._calls)

    @property
    def has_api_key(self) -> bool:
        """是否可解析到 API Key（不暴露 Key 本身）。"""
        return bool(self._resolve_api_key(required=False))

    def clear_calls(self) -> None:
        self._calls.clear()

    def last_latency_ms(self) -> int | None:
        return self._calls[-1].latency_ms if self._calls else None

    async def __call__(self, request: PredictionRequest) -> str:
        """非阻塞入口：阻塞 HTTP 在线程中执行，事件循环不被占用。"""
        return await asyncio.to_thread(self._call_blocking, request)

    def _resolve_api_key(self, *, required: bool = True) -> str:
        key = self._api_key if self._api_key is not None else os.environ.get(self._api_key_env, "")
        if not key:
            if required:
                raise PredictionTransportError(f"{self._api_key_env} is not set; refusing to call OpenRouter")
            return ""
        return key

    def _call_blocking(self, request: PredictionRequest) -> str:
        telemetry = _CallTelemetry(
            body=build_chat_completions_body(request, model=self._model),
            started=time.monotonic(),
        )
        try:
            self._post(telemetry)
            telemetry.content = extract_message_content(telemetry.attempt.body)
            return telemetry.content
        except PredictionError as exc:
            telemetry.error = type(exc).__name__
            raise
        except Exception as exc:  # 兜底：任何未预期异常都不能变成假预测
            telemetry.error = PredictionTransportError.__name__
            raise PredictionTransportError(
                f"OpenRouter transport raised {type(exc).__name__}: {exc}"
            ) from exc
        finally:
            self._calls.append(
                OpenRouterCall(
                    endpoint=self._endpoint,
                    model=self._model,
                    request_body=telemetry.body,
                    http_status=telemetry.attempt.status,
                    response_body=telemetry.attempt.body,
                    content=telemetry.content,
                    latency_ms=int((time.monotonic() - telemetry.started) * 1_000),
                    error=telemetry.error,
                )
            )

    def _post(self, telemetry: _CallTelemetry) -> None:
        """执行一次 HTTP POST，成功时把 status/body 写入 telemetry。"""
        api_key = self._resolve_api_key()
        http_request = urllib.request.Request(
            self._endpoint,
            data=telemetry.body.encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(http_request, timeout=self._timeout_s) as response:
                telemetry.attempt.status = int(getattr(response, "status", 200))
                telemetry.attempt.body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            telemetry.attempt.status = int(exc.code)
            telemetry.attempt.body = _read_error_body(exc)
            raise map_http_status(telemetry.attempt.status, response_excerpt=telemetry.attempt.body) from None
        except TimeoutError as exc:
            raise PredictionTimeoutError(f"OpenRouter did not respond within {self._timeout_s} s") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise PredictionTimeoutError(
                    f"OpenRouter did not respond within {self._timeout_s} s"
                ) from exc
            raise PredictionTransportError(
                f"OpenRouter transport failed: {type(exc.reason).__name__}: {exc.reason}"
            ) from exc


def _read_error_body(error: urllib.error.HTTPError) -> str:
    """读取错误响应体并关闭它（避免 HTTPError 作为 file-like 对象触发 ResourceWarning）。"""
    try:
        return error.read().decode("utf-8", "replace")
    except Exception:
        return ""
    finally:
        try:
            error.close()
        except Exception:  # pragma: no cover - 关闭失败不影响错误映射
            pass


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "OPENROUTER_API_KEY_ENV",
    "OPENROUTER_ENDPOINT",
    "OPENROUTER_MODEL",
    "OpenRouterCall",
    "OpenRouterTransport",
    "build_chat_completions_body",
    "extract_message_content",
    "map_http_status",
]
