"""私有 REST 边界（P0001.9.2）：签名请求 + listenKey 管理。

**唯一的网络/wall-clock 边界之一**（与 `auth.py` 的测量助手、`runtime.py` 并列）。

安全纪律：

- 请求头只带 `X-MBX-APIKEY`；**错误消息只报 method + path + HTTP 状态，绝不带 query string**（签名等价凭证）；
- 响应解析严格；非 JSON / 缺字段一律 `PrivateFormatError`；
- 本模块没有任何下单/撤单端点（SC-14，由静态测试固定）。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from dataclasses import field as dataclasses_field, field
from typing import Mapping, Protocol

from connectors.binance.market_data.endpoints import (
    ACCOUNT_PATH,
    ALL_ORDERS_PATH,
    INCOME_PATH,
    LISTEN_KEY_PATH,
    OPEN_ORDERS_PATH,
    POSITION_RISK_PATH,
    REST_BASE_URL,
    SERVER_TIME_PATH,
    USER_TRADES_PATH,
)
from connectors.binance.market_data.errors import TransportError
from connectors.binance.private.auth import ApiCredentials, ClockCalibration, ServerTimeOffset, build_signed_request, wall_clock_ms
from connectors.binance.private.errors import PrivateFormatError, PrivateResponseError

#: 认证头（值只在内存中传递，绝不打印）。
API_KEY_HEADER = "X-MBX-APIKEY"


class RestFetcher(Protocol):
    """注入式 HTTP JSON 契约（便于离线测试）。"""

    def send(
        self, *, method: str, url: str, headers: Mapping[str, str], timeout_s: float
    ) -> object: ...


@dataclass
class UrllibRestFetcher:
    """标准库实现（`urllib.request`）；异常消息不含 query（遮蔽签名）。"""

    #: 最近一次响应的真实头（供 usage fact 采集；additive）
    last_headers: dict[str, str] = dataclasses_field(default_factory=dict)

    def send(self, *, method: str, url: str, headers: Mapping[str, str], timeout_s: float) -> object:
        parts = urllib.parse.urlsplit(url)
        safe_target = f"{parts.scheme}://{parts.netloc}{parts.path}"
        request = urllib.request.Request(url, headers=dict(headers), method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as handle:
                body = handle.read()
                self.last_headers = dict(handle.headers.items())
        except urllib.error.HTTPError as exc:
            raise PrivateResponseError(f"{method} {safe_target} -> HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise TransportError(f"{method} {safe_target} failed: {type(exc).__name__}") from exc
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PrivateFormatError(f"{method} {safe_target}: response is not valid JSON") from exc


@dataclass
class PrivateRestClient:
    """签名 REST 客户端 + listenKey 管理。"""

    credentials: ApiCredentials
    fetcher: RestFetcher
    base_url: str = REST_BASE_URL
    recv_window_ms: int = 5_000
    timeout_s: float = 10.0
    offset: ServerTimeOffset = field(default_factory=ServerTimeOffset)
    clock: object = wall_clock_ms

    def __repr__(self) -> str:
        return f"PrivateRestClient(base_url={self.base_url!r}, recv_window_ms={self.recv_window_ms})"

    __str__ = __repr__

    # ------------------------------------------------------------------ 基础

    def _headers(self) -> dict[str, str]:
        return {API_KEY_HEADER: self.credentials.api_key, "Accept": "application/json"}

    def _url(self, path: str, query: str | None = None) -> str:
        base = f"{self.base_url.rstrip('/')}{path}"
        return f"{base}?{query}" if query else base

    def _now_ms(self) -> int:
        return self.offset.timestamp_ms(local_now_ms=int(self.clock()))  # type: ignore[operator]

    # ------------------------------------------------------------------ 公开端点

    def measure_clock(self) -> "ClockCalibration":
        """测量并返回完整 `ClockCalibration`（偏移 / RTT / 不确定度 / 测量时刻）。

        P0001.9.4 §6：readiness 需要不确定度与新鲜度，不能只有一个 offset 数字。
        """
        started = int(self.clock())  # type: ignore[operator]
        raw = self.fetcher.send(
            method="GET", url=self._url(SERVER_TIME_PATH), headers={"Accept": "application/json"}, timeout_s=self.timeout_s
        )
        local_receive = int(self.clock())  # type: ignore[operator]
        if not isinstance(raw, dict) or "serverTime" not in raw:
            raise PrivateFormatError("server time response must be an object with 'serverTime'")
        value = raw["serverTime"]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise PrivateFormatError(f"serverTime must be a positive int, got {value!r}")
        self.offset.measure(
            server_time_ms=value, local_receive_ms=local_receive, round_trip_ms=max(0, local_receive - started)
        )
        calibration = self.offset.calibration
        if calibration is None:  # pragma: no cover —— measure() 必然写入
            raise PrivateFormatError("clock calibration was not recorded")
        return calibration

    def measure_server_time(self) -> int:
        """测量 `server_time - local_time` 并记录到 `offset`。"""
        started = int(self.clock())  # type: ignore[operator]
        raw = self.fetcher.send(
            method="GET", url=self._url(SERVER_TIME_PATH), headers={"Accept": "application/json"}, timeout_s=self.timeout_s
        )
        local_receive = int(self.clock())  # type: ignore[operator]
        if not isinstance(raw, dict) or "serverTime" not in raw:
            raise PrivateFormatError("server time response must be an object with 'serverTime'")
        value = raw["serverTime"]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise PrivateFormatError(f"serverTime must be a positive int, got {value!r}")
        return self.offset.measure(
            server_time_ms=value, local_receive_ms=local_receive, round_trip_ms=max(0, local_receive - started)
        )

    # ------------------------------------------------------------------ 签名端点

    def account_snapshot(self) -> object:
        return self._signed_get(ACCOUNT_PATH, {})

    def position_risk(self) -> object:
        return self._signed_get(POSITION_RISK_PATH, {})

    # ------------------------------------------------------------------ 启动恢复（只读，P0001.9.3）

    def open_orders(self, symbol: str) -> object:
        """`GET /fapi/v1/openOrders` —— 当前挂单（只读）。"""
        return self._signed_get(OPEN_ORDERS_PATH, {"symbol": _require_symbol(symbol)})

    def order_history(self, symbol: str, *, limit: int) -> object:
        """`GET /fapi/v1/allOrders` —— 历史订单（只读；用于发现终态与 clientOrderId 映射）。"""
        return self._signed_get(ALL_ORDERS_PATH, {"symbol": _require_symbol(symbol), "limit": _require_limit(limit)})

    def user_trades(self, symbol: str, *, limit: int) -> object:
        """`GET /fapi/v1/userTrades` —— 账户成交（只读）。"""
        return self._signed_get(USER_TRADES_PATH, {"symbol": _require_symbol(symbol), "limit": _require_limit(limit)})

    # ------------------------------------------------------------------ 历史风险（只读，P0001.9.4.1）

    def income_history(
        self, *, start_time: int, end_time: int, page: int, limit: int
    ) -> object:
        """`GET /fapi/v1/income` —— 账户级收入历史（只读；**不传 symbol**）。

        - 账户级而非单 symbol：同一合约账户的其他交易对损益同样影响账户风险；
        - `page`（1 起）与 `limit`（<= 1000）由调用方显式给出（分页必须能被完整驱动）。
        """
        for name, value in (("start_time", start_time), ("end_time", end_time)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise PrivateFormatError(f"income_history {name} must be a non-negative int")
        if start_time > end_time:
            raise PrivateFormatError("income_history start_time must be <= end_time")
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            raise PrivateFormatError("income_history page must be an int >= 1")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise PrivateFormatError("income_history limit must be in [1, 1000]")
        return self._signed_get(
            INCOME_PATH,
            {"startTime": start_time, "endTime": end_time, "page": page, "limit": limit},
        )

    def _signed_get(self, path: str, params: Mapping[str, object]) -> object:
        signed = build_signed_request(
            path=path,
            params=params,
            credentials=self.credentials,
            recv_window_ms=self.recv_window_ms,
            timestamp_ms=self._now_ms(),
        )
        return self.fetcher.send(
            method="GET", url=self._url(path, signed.query), headers=self._headers(), timeout_s=self.timeout_s
        )

    # ------------------------------------------------------------------ listenKey

    def listen_key_create(self) -> str:
        """`POST /fapi/v1/listenKey` → listenKey（视为敏感，不打印）。"""
        raw = self._key_request("POST", LISTEN_KEY_PATH)
        if not isinstance(raw, dict) or "listenKey" not in raw:
            raise PrivateFormatError("listenKey create response must contain 'listenKey'")
        listen_key = raw["listenKey"]
        if not isinstance(listen_key, str) or not listen_key:
            raise PrivateFormatError("listenKey must be a non-empty string")
        return listen_key

    def listen_key_keepalive(self) -> None:
        """`PUT /fapi/v1/listenKey`（续期）。"""
        self._key_request("PUT", LISTEN_KEY_PATH)

    def listen_key_close(self) -> None:
        """`DELETE /fapi/v1/listenKey`（关闭）。"""
        self._key_request("DELETE", LISTEN_KEY_PATH)

    def _key_request(self, method: str, path: str) -> object:
        return self.fetcher.send(
            method=method, url=self._url(path), headers=self._headers(), timeout_s=self.timeout_s
        )


def _require_symbol(symbol: object) -> str:
    if not isinstance(symbol, str) or not symbol:
        raise PrivateFormatError("symbol must be a non-empty string")
    return symbol


def _require_limit(limit: object) -> str:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise PrivateFormatError(f"limit must be an int in [1, 1000], got {limit!r}")
    return str(limit)


__all__ = ["API_KEY_HEADER", "PrivateRestClient", "RestFetcher", "UrllibRestFetcher"]
