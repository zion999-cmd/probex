"""REST 客户端：深度快照 / exchangeInfo / server time（P0001.9.1 §0.2）。

wall-clock 与网络只允许出现在本模块；返回的都是 canonical 值（`MarketEvent` / `TradingRules` / 整数）。

HTTP 客户端是**注入式**的（`JsonHttpClient` 是默认实现，测试可替换），
因此解析与对齐逻辑可以完全离线测试。
"""

from __future__ import annotations

import dataclasses
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Protocol

from market.events.types import MarketEvent, Milliseconds

from connectors.binance.market_data.depth import parse_depth_snapshot
from connectors.binance.market_data.endpoints import DEPTH_PATH, EXCHANGE_INFO_PATH, REST_BASE_URL, SERVER_TIME_PATH
from connectors.binance.market_data.errors import MarketDataFormatError, TransportError
from connectors.binance.market_data.exchange_info import TradingRules, parse_exchange_info
from connectors.binance.market_data.parsing import require_field, require_int, require_mapping

#: 默认 wall-clock（毫秒）。
def wall_clock_ms() -> Milliseconds:
    return int(time.time() * 1000)


class JsonHttpClient(Protocol):
    """最小 HTTP JSON 契约（便于注入 stub）。"""

    def get_json(self, path: str, params: dict[str, str], *, timeout_s: float) -> object: ...


@dataclass
class UrllibJsonClient:
    """标准库 HTTP 客户端（`urllib.request`）。"""

    base_url: str = REST_BASE_URL
    opener: Callable[[urllib.request.Request, float], object] | None = None
    #: 最近一次响应的真实头（供 usage fact 采集；additive，不改请求语义）
    last_headers: dict[str, str] = dataclasses.field(default_factory=dict)

    def get_json(self, path: str, params: dict[str, str], *, timeout_s: float) -> object:
        query = urllib.parse.urlencode(params)
        url = f"{self.base_url.rstrip('/')}{path}"
        if query:
            url = f"{url}?{query}"
        request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
        try:
            if self.opener is not None:
                response = self.opener(request, timeout_s)
                body = response.read()  # type: ignore[attr-defined]
                self.last_headers = dict(getattr(response, "headers", {}) or {})
            else:
                with urllib.request.urlopen(request, timeout=timeout_s) as handle:
                    body = handle.read()
                    self.last_headers = dict(handle.headers.items())
        except urllib.error.HTTPError as exc:
            raise TransportError(f"HTTP {exc.code} for {url}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise TransportError(f"request to {url} failed: {exc}") from exc
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MarketDataFormatError(f"{url}: response is not valid JSON: {exc}") from None


@dataclass
class DepthSnapshotClient:
    """深度快照客户端：`GET /fapi/v1/depth`。"""

    client: JsonHttpClient
    symbol: str
    limit: int
    timeout_s: float
    clock: Callable[[], Milliseconds] = wall_clock_ms

    def fetch(self) -> MarketEvent:
        """拉取快照并把响应归一化为 `BOOK_SNAPSHOT` 事件。"""
        receive_ts = self.clock()
        raw = self.client.get_json(
            DEPTH_PATH, {"symbol": self.symbol, "limit": str(self.limit)}, timeout_s=self.timeout_s
        )
        return parse_depth_snapshot(raw, symbol=self.symbol, receive_ts=receive_ts, process_ts=self.clock())


@dataclass
class ExchangeInfoClient:
    """exchangeInfo 客户端：`GET /fapi/v1/exchangeInfo`。"""

    client: JsonHttpClient
    symbol: str
    timeout_s: float

    def fetch(self) -> TradingRules:
        raw = self.client.get_json(EXCHANGE_INFO_PATH, {}, timeout_s=self.timeout_s)
        return parse_exchange_info(raw, symbol=self.symbol)


@dataclass
class ServerTimeClient:
    """服务器时间客户端：`GET /fapi/v1/time`。"""

    client: JsonHttpClient
    timeout_s: float
    clock: Callable[[], Milliseconds] = wall_clock_ms

    def fetch_offset_ms(self) -> int:
        """返回 `server_time - local_time`（仅用于观测，**不改写**源事件时间）。"""
        local_before = self.clock()
        raw = self.client.get_json(SERVER_TIME_PATH, {}, timeout_s=self.timeout_s)
        message = require_mapping(raw, path="serverTime")
        server_time = require_int(require_field(message, "serverTime", path="serverTime"), path="serverTime.serverTime")
        return server_time - local_before


__all__ = [
    "DepthSnapshotClient",
    "ExchangeInfoClient",
    "JsonHttpClient",
    "ServerTimeClient",
    "UrllibJsonClient",
    "wall_clock_ms",
]
