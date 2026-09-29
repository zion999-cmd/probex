"""Binance USDⓈ-M **写**执行 REST 客户端（P0001.9.6 §16 / §17）。

与 `connectors.binance.private.rest.PrivateRestClient`（只读）**显式分开**。本模块只做三件事：

```text
POST   /fapi/v1/order   (LIMIT + GTX，newClientOrderId = Order.client_order_id)
DELETE /fapi/v1/order   (origClientOrderId)
GET    /fapi/v1/order   (origClientOrderId —— 解决 uncertain submit/cancel)
```

**结果分类是结构化的**（§5 / SC-8）：

| 情况 | 异常 | submit 分类 |
| --- | --- | --- |
| HTTP 4xx 且响应体含业务错误码（交易所**确实处理了**请求） | `ExecutionRequestRejected` | CONFIRMED_REJECTED |
| HTTP 5xx / 429 / 408 / 响应体无法解析 | `ExecutionOutcomeUnknown` | UNKNOWN |
| 超时 / 连接中断 / TLS 失败 | `TransportError` | UNKNOWN |

错误信息里**只有** `method + path + 状态码 + 业务 code/msg`，**绝不**包含 signed query、signature、API key（§17）。
"""

from __future__ import annotations

from decimal import Decimal

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Mapping, Protocol

from connectors.binance.market_data.errors import TransportError
from connectors.binance.private.auth import (
    ApiCredentials,
    ServerTimeOffset,
    build_signed_request,
    wall_clock_ms,
)
from connectors.binance.private.errors import PrivateFormatError, PrivateResponseError
from connectors.binance.private.rest import API_KEY_HEADER

#: 写路径端点唯一 Owner。
ORDER_PATH = "/fapi/v1/order"
#: 第一版唯一允许的 time-in-force（post-only）。
POST_ONLY_TIME_IN_FORCE = "GTX"
#: one-way 模式的 positionSide。
ONE_WAY_POSITION_SIDE = "BOTH"
#: 视为"结果未知"的 HTTP 状态（5xx / 限流 / 请求超时）。
_UNKNOWN_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class ExecutionRequestRejected(PrivateResponseError):
    """交易所**明确**业务拒绝（含 `code` / `msg`；请求已被处理）。"""

    def __init__(self, *, status: int, code: int, message: str) -> None:
        super().__init__(f"order request rejected (HTTP {status}, code {code}): {message[:200]}")
        self.status = status
        self.code = code
        self.message = message[:200]


class ExecutionOutcomeUnknown(PrivateResponseError):
    """结果**未知**（5xx / 限流 / 响应不可解析）⇒ 上层必须按 uncertain 处理，**不得**重试 submit。"""

    def __init__(self, *, status: int | None, detail: str) -> None:
        super().__init__(f"order request outcome unknown (HTTP {status}): {detail}"[:240])
        self.status = status
        self.detail = detail


class ExecutionHttpFetcher(Protocol):
    """注入式 HTTP 契约（测试用假实现）。"""

    def send(self, *, method: str, url: str, headers: Mapping[str, str], timeout_s: float) -> object: ...


@dataclass
class UrllibExecutionFetcher:
    """标准库实现；按状态码给出**结构化**结果（异常消息不含 query）。"""

    def send(self, *, method: str, url: str, headers: Mapping[str, str], timeout_s: float) -> object:
        parts = urllib.parse.urlsplit(url)
        safe_target = f"{parts.scheme}://{parts.netloc}{parts.path}"
        request = urllib.request.Request(url, headers=dict(headers), method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as handle:
                body = handle.read()
        except urllib.error.HTTPError as exc:
            raise _http_error(exc, safe_target=safe_target) from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise TransportError(f"{method} {safe_target} failed: {type(exc).__name__}") from None
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ExecutionOutcomeUnknown(status=None, detail="response body is not valid JSON") from None


def _http_error(exc: "urllib.error.HTTPError", *, safe_target: str) -> BaseException:
    """把 HTTP 错误映射成**结构化**结果（不泄露 query / signature）。"""
    status = int(exc.code)
    try:
        body = exc.read().decode("utf-8")
        payload = json.loads(body)
    except Exception:  # noqa: BLE001 - 响应体不可读 ⇒ 结果未知
        payload = None
    if isinstance(payload, dict) and "code" in payload and status not in _UNKNOWN_STATUSES:
        code = payload.get("code")
        message = payload.get("msg") or payload.get("message") or ""
        if isinstance(code, int) and not isinstance(code, bool):
            return ExecutionRequestRejected(status=status, code=code, message=str(message))
    if status in _UNKNOWN_STATUSES or status >= 500:
        return ExecutionOutcomeUnknown(status=status, detail=f"{safe_target} -> HTTP {status}")
    # 4xx 但没有可解析业务码：交易所**可能**已处理，但无法确认 ⇒ 未知
    return ExecutionOutcomeUnknown(status=status, detail=f"{safe_target} -> HTTP {status} (no business code)")


@dataclass
class BinanceExecutionRestClient:
    """写路径 REST 客户端（只暴露 submit / cancel / query）。"""

    credentials: ApiCredentials
    fetcher: ExecutionHttpFetcher
    base_url: str
    recv_window_ms: int = 5_000
    timeout_s: float = 10.0
    offset: ServerTimeOffset = field(default_factory=ServerTimeOffset)
    clock: Callable[[], int] = wall_clock_ms

    def __repr__(self) -> str:
        return f"BinanceExecutionRestClient(base_url={self.base_url!r}, recv_window_ms={self.recv_window_ms})"

    __str__ = __repr__

    # ------------------------------------------------------------------ 写

    def submit_post_only_limit(
        self,
        *,
        symbol: str,
        side: OrderSide,
        quantity: float,
        price: float,
        client_order_id: str,
        reduce_only: bool,
    ) -> object:
        """`POST /fapi/v1/order`：LIMIT + GTX + positionSide=BOTH（唯一允许的组合）。"""
        if not isinstance(side, OrderSide):
            raise PrivateFormatError("submit side must be an OrderSide")
        if not isinstance(reduce_only, bool):
            raise PrivateFormatError("submit reduce_only must be a bool")
        params: dict[str, object] = {
            "symbol": _require_text(symbol, "symbol"),
            "side": side.value,
            "type": "LIMIT",
            "timeInForce": POST_ONLY_TIME_IN_FORCE,
            "quantity": _require_number(quantity, "quantity"),
            "price": _require_number(price, "price"),
            "newClientOrderId": _require_text(client_order_id, "newClientOrderId"),
            "positionSide": ONE_WAY_POSITION_SIDE,
            "newOrderRespType": "RESULT",
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        return self._signed_request("POST", params)

    def cancel_order(self, *, symbol: str, client_order_id: str) -> object:
        """`DELETE /fapi/v1/order`（按 `origClientOrderId`）。"""
        return self._signed_request(
            "DELETE",
            {
                "symbol": _require_text(symbol, "symbol"),
                "origClientOrderId": _require_text(client_order_id, "origClientOrderId"),
            },
        )

    # ------------------------------------------------------------------ 只读（解决不确定结果）

    def query_order(self, *, symbol: str, client_order_id: str) -> object:
        """`GET /fapi/v1/order`（按 `origClientOrderId`；UNKNOWN 时不需要 orderId）。"""
        return self._signed_request(
            "GET",
            {
                "symbol": _require_text(symbol, "symbol"),
                "origClientOrderId": _require_text(client_order_id, "origClientOrderId"),
            },
        )

    # ------------------------------------------------------------------ 内部

    def _signed_request(self, method: str, params: Mapping[str, object]) -> object:
        signed = build_signed_request(
            path=ORDER_PATH,
            params=params,
            credentials=self.credentials,
            recv_window_ms=self.recv_window_ms,
            timestamp_ms=self.offset.timestamp_ms(local_now_ms=int(self.clock())),
        )
        return self.fetcher.send(
            method=method,
            url=f"{self.base_url.rstrip('/')}{ORDER_PATH}?{signed.query}",
            headers=self._headers(),
            timeout_s=self.timeout_s,
        )

    def _headers(self) -> dict[str, str]:
        return {API_KEY_HEADER: self.credentials.api_key, "Accept": "application/json"}


def _require_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise PrivateFormatError(f"{name} must be a non-empty string")
    return value


def _require_number(value: object, name: str) -> str:
    """把订单数值序列化成请求字符串。

    P0001.9.7.2：接受 `Decimal`（精确十进制，直接 `format(value, 'f')` 输出，**不**经 float 往返），
    以及 `int` / `float`（保持既有行为：`repr(float(v))`）。
    """
    if isinstance(value, bool) or isinstance(value, str) or value is None:
        raise PrivateFormatError(f"{name} must be a positive number")
    if isinstance(value, Decimal):
        if not value.is_finite() or value <= 0:
            raise PrivateFormatError(f"{name} must be a positive finite number")
        return format(value, "f")
    if not isinstance(value, (int, float)) or value <= 0:
        raise PrivateFormatError(f"{name} must be a positive number")
    return repr(float(value))


__all__ = [
    "ORDER_PATH",
    "ONE_WAY_POSITION_SIDE",
    "POST_ONLY_TIME_IN_FORCE",
    "BinanceExecutionRestClient",
    "ExecutionHttpFetcher",
    "ExecutionOutcomeUnknown",
    "ExecutionRequestRejected",
    "OrderSide",
    "UrllibExecutionFetcher",
]
