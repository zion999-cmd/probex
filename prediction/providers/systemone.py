"""Native Typed Jev（OpenRouter System One）transport 与 provider。

```text
MarketState → jev-market-v1 domain payload → SystemOneProvider（wire 适配）
    → POST /api/v1/systemone → answers.<qid> → TypedJevParser → domain answers JSON
    → 既有 strict parser → 现有 Prediction / PredictionRecord
```

职责边界（P0001.4.2）：

- `SystemOneTransport`：HTTP / Authorization / timeout / envelope / HTTP status / transport error / telemetry。
- `SystemOneProvider`：**wire ⇄ domain 适配** —— 用 `prediction/systemone_wire.py` 构造
  `{model, state, questions}`，把 typed answers 变回 `jev-market-v1` domain answers，再交给既有 parser。
- 不修改 `MarketState`、Scheduler、`jev-market-v1` 业务语义；不使用 Chat Completions / `typesafe/jev-router`；
  不做 prompt engineering、不解析自然语言、不做 markdown 提取；不使用 Score 表达 adverse-selection。

安全：API Key 只从环境变量 `OPENROUTER_API_KEY` 读取，只放进请求头；绝不进入 telemetry / 异常 / Record / 文件。

时间：wall-clock（`time.monotonic`）只用于 latency telemetry，且只允许出现在 transport 模块。
"""

from __future__ import annotations

import asyncio
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from prediction.errors import (
    PredictionError,
    PredictionTimeoutError,
    PredictionTransportError,
)
from prediction.parsing.systemone import SystemOneAnswers, parse_systemone_response
from prediction.providers.base import ProviderResponse
from prediction.providers.http_errors import map_http_status
from prediction.schema.market_v1 import QUESTION_SCHEMA_VERSION, canonical_json
from prediction.systemone_wire import (
    DEFAULT_TIMEOUT_S,
    SYSTEMONE_API_KEY_ENV,
    SYSTEMONE_BASE_URL,
    SYSTEMONE_MODEL_ALIAS,
    SYSTEMONE_PATH,
    build_systemone_body,
)
from prediction.types import PredictionRequest


@dataclass(frozen=True, slots=True)
class SystemOneCall:
    """一次 System One 调用的 telemetry（不含任何凭证）。"""

    endpoint: str
    model: str
    request_body: str
    http_status: int | None
    response_body: str
    latency_ms: int
    error: str | None

    @property
    def request_bytes(self) -> int:
        return len(self.request_body.encode("utf-8"))

    @property
    def response_bytes(self) -> int:
        return len(self.response_body.encode("utf-8"))

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
            "latency_ms": self.latency_ms,
            "error": self.error,
        }


@dataclass
class _Attempt:
    status: int | None = None
    body: str = ""


@dataclass
class _Telemetry:
    body: str
    started: float
    attempt: _Attempt = field(default_factory=_Attempt)
    error: str | None = None


class SystemOneTransport:
    """HTTP transport：`POST {base}/v1/systemone`。"""

    def __init__(
        self,
        *,
        base_url: str = SYSTEMONE_BASE_URL,
        path: str = SYSTEMONE_PATH,
        api_key: str | None = None,
        api_key_env: str = SYSTEMONE_API_KEY_ENV,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must be an http(s) URL")
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("path must start with '/'")
        if not isinstance(api_key_env, str) or not api_key_env:
            raise ValueError("api_key_env must be a non-empty string")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be > 0")
        if api_key is not None and (not isinstance(api_key, str) or not api_key):
            raise ValueError("api_key must be a non-empty string or None")

        self._base_url = base_url.rstrip("/")
        self._path = path
        self._api_key = api_key
        self._api_key_env = api_key_env
        self._timeout_s = timeout_s
        self._calls: list[SystemOneCall] = []

    @property
    def endpoint(self) -> str:
        return f"{self._base_url}{self._path}"

    @property
    def timeout_s(self) -> float:
        return self._timeout_s

    @property
    def calls(self) -> tuple[SystemOneCall, ...]:
        return tuple(self._calls)

    @property
    def has_api_key(self) -> bool:
        return bool(self._resolve_api_key(required=False))

    def clear_calls(self) -> None:
        self._calls.clear()

    def last_latency_ms(self) -> int | None:
        return self._calls[-1].latency_ms if self._calls else None

    async def post(self, body: str, *, model: str) -> str:
        """非阻塞入口：阻塞 HTTP 在线程中执行。失败抛既有 failure type。"""
        return await asyncio.to_thread(self._post_blocking, body, model)

    def _resolve_api_key(self, *, required: bool = True) -> str:
        key = self._api_key if self._api_key is not None else os.environ.get(self._api_key_env, "")
        if not key:
            if required:
                raise PredictionTransportError(f"{self._api_key_env} is not set; refusing to call System One")
            return ""
        return key

    def _post_blocking(self, body: str, model: str) -> str:
        telemetry = _Telemetry(body=body, started=time.monotonic())
        try:
            self._send(telemetry)
            return telemetry.attempt.body
        except PredictionError as exc:
            telemetry.error = type(exc).__name__
            raise
        except Exception as exc:  # 兜底：未预期异常不得变成假预测
            telemetry.error = PredictionTransportError.__name__
            raise PredictionTransportError(
                f"System One transport raised {type(exc).__name__}: {exc}"
            ) from exc
        finally:
            self._calls.append(
                SystemOneCall(
                    endpoint=self.endpoint,
                    model=model,
                    request_body=telemetry.body,
                    http_status=telemetry.attempt.status,
                    response_body=telemetry.attempt.body,
                    latency_ms=int((time.monotonic() - telemetry.started) * 1_000),
                    error=telemetry.error,
                )
            )

    def _send(self, telemetry: _Telemetry) -> None:
        api_key = self._resolve_api_key()
        http_request = urllib.request.Request(
            self.endpoint,
            data=telemetry.body.encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(http_request, timeout=self._timeout_s) as response:
                telemetry.attempt.status = int(getattr(response, "status", 200))
                telemetry.attempt.body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            telemetry.attempt.status = int(exc.code)
            telemetry.attempt.body = _read_error_body(exc)
            raise map_http_status(
                telemetry.attempt.status, response_excerpt=telemetry.attempt.body, provider="System One"
            ) from None
        except TimeoutError as exc:
            raise PredictionTimeoutError(f"System One did not respond within {self._timeout_s} s") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise PredictionTimeoutError(f"System One did not respond within {self._timeout_s} s") from exc
            raise PredictionTransportError(
                f"System One transport failed: {type(exc.reason).__name__}: {exc.reason}"
            ) from exc


class SystemOneProvider:
    """`PredictionProvider`：domain payload ⇄ System One typed wire 适配。"""

    provider_name = "typesafe"

    def __init__(
        self,
        transport: SystemOneTransport | None = None,
        *,
        model: str = SYSTEMONE_MODEL_ALIAS,
        adverse_selection_threshold_bps: float,
        horizons_ms: tuple[int, ...] | None = None,
        question_schema_version: str = QUESTION_SCHEMA_VERSION,
    ) -> None:
        if not isinstance(model, str) or not model:
            raise ValueError("model must be a non-empty string")
        if (
            isinstance(adverse_selection_threshold_bps, bool)
            or not isinstance(adverse_selection_threshold_bps, (int, float))
            or adverse_selection_threshold_bps <= 0
        ):
            raise ValueError(
                "adverse_selection_threshold_bps must be a positive number "
                "(提案未定义该业务阈值，必须由调用方显式给出)"
            )
        if horizons_ms is None:
            from prediction.schema.market_v1 import FUTURE_RETURN_HORIZONS_MS

            horizons_ms = FUTURE_RETURN_HORIZONS_MS
        if not horizons_ms:
            raise ValueError("horizons_ms must not be empty")

        self._transport = transport if transport is not None else SystemOneTransport()
        self._model = model
        self._threshold_bps = float(adverse_selection_threshold_bps)
        self._horizons_ms = tuple(horizons_ms)
        self._question_schema_version = question_schema_version

    @property
    def model(self) -> str:
        return self._model

    @property
    def transport(self) -> SystemOneTransport:
        return self._transport

    @property
    def adverse_selection_threshold_bps(self) -> float:
        return self._threshold_bps

    @property
    def horizons_ms(self) -> tuple[int, ...]:
        return self._horizons_ms

    async def predict(self, request: PredictionRequest) -> ProviderResponse:
        body = build_systemone_body(
            request.payload_json,
            model=self._model,
            horizons_ms=self._horizons_ms,
            adverse_selection_threshold_bps=self._threshold_bps,
        )
        raw = await self._transport.post(body, model=self._model)
        answers: SystemOneAnswers = parse_systemone_response(
            raw,
            horizons_ms=self._horizons_ms,
            question_schema_version=self._question_schema_version,
        )
        return ProviderResponse(
            provider=answers.provider or self.provider_name,
            model=self._model,
            raw_response=canonical_json(answers.domain_answers),
            requested_model=self._model,
            resolved_model=answers.resolved_model,
            response_id=answers.response_id,
            usage=answers.usage,
        )

    async def __call__(self, request: PredictionRequest) -> ProviderResponse:
        return await self.predict(request)


def _read_error_body(error: urllib.error.HTTPError) -> str:
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
    "SYSTEMONE_API_KEY_ENV",
    "SYSTEMONE_BASE_URL",
    "SYSTEMONE_MODEL_ALIAS",
    "SYSTEMONE_PATH",
    "SystemOneCall",
    "SystemOneProvider",
    "SystemOneTransport",
]
