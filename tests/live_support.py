"""P0001.9.1 测试脚手架：注入式传输 / HTTP 与脚本化报文。

- `FakeConnection` / `ScriptedTransport`：无需网络即可驱动 `LiveMarketDataRuntime`；
- `FakeHttp`：注入 `exchangeInfo` / `depth` / `time` 响应；
- `*_message(...)`：构造与 Binance 报文同形的 JSON 文本。
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field

from connectors.binance.market_data.errors import MarketDataFormatError, TransportError, WebSocketClosed, WebSocketTimeout
from connectors.binance.market_data.streams import depth_stream
from connectors.binance.market_data.transport import ReconnectPolicy
from tests.support import BASE_TS

BINANCE_SYMBOL = "BTCUSDT"


def live_config(**overrides: object):
    """测试用 `LiveMarketDataConfig`（数值都是测试值）。"""
    from connectors.binance.market_data.runtime import LiveMarketDataConfig

    values: dict[str, object] = {
        "symbol": BINANCE_SYMBOL,
        "depth_speed": "",  # 默认速度（实测 100ms 变体 U/u 空洞最多；250ms 后缀无效）
        "mark_price_speed": "1s",
        "depth_limit": 1000,
        "connect_timeout_s": 1.0,
        "read_timeout_s": 0.5,
        "snapshot_timeout_s": 1.0,
        "exchange_info_timeout_s": 1.0,
        "reconnect": ReconnectPolicy(max_attempts=3, base_backoff_ms=1, max_backoff_ms=2),
        "resync_cooldown_ms": 0,
        "history_limit": 64,
    }
    values.update(overrides)
    return LiveMarketDataConfig(**values)  # type: ignore[arg-type]


@dataclass
class FakeConnection:
    """脚本化的 WS 连接。"""

    url: str
    messages: deque[str] = field(default_factory=deque)
    sent: list[str] = field(default_factory=list)
    closed: bool = False
    dropped: bool = False
    timeouts: int = 0

    def send_text(self, text: str) -> None:
        if self.closed or self.dropped:
            raise WebSocketClosed("connection is not usable")
        self.sent.append(text)

    def recv_text(self, *, timeout_s: float) -> str | None:
        if self.dropped:
            raise WebSocketClosed("peer closed the connection")
        if self.closed:
            return None
        if not self.messages:
            self.timeouts += 1
            raise WebSocketTimeout("no complete message within the timeout")
        return self.messages.popleft()

    def close(self) -> None:
        self.closed = True

    def push(self, text: str) -> None:
        self.messages.append(text)

    def drop(self) -> None:
        self.dropped = True


@dataclass
class ScriptedTransport:
    """注入式传输工厂：按 tier 路径分配连接。"""

    connections: dict[str, FakeConnection] = field(default_factory=dict)
    urls: list[str] = field(default_factory=list)
    failures: deque[str] = field(default_factory=deque)

    def __call__(self, url: str, *, timeout_s: float):
        self.urls.append(url)
        if self.failures:
            raise TransportError(self.failures.popleft())
        tier = "public" if "/public/" in url else "market"
        connection = self.connections.get(tier)
        if connection is None:
            connection = FakeConnection(url=url)
            self.connections[tier] = connection
        else:
            connection.url = url
            connection.closed = False
            connection.dropped = False
        return connection

    def connection(self, tier: str) -> FakeConnection:
        return self.connections[tier]


@dataclass
class FakeHttp:
    """注入式 HTTP JSON 客户端。"""

    responses: dict[str, object]
    calls: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    failures: dict[str, int] = field(default_factory=dict)

    def get_json(self, path: str, params: dict[str, str], *, timeout_s: float) -> object:
        self.calls.append((path, dict(params)))
        remaining = self.failures.get(path, 0)
        if remaining > 0:
            self.failures[path] = remaining - 1
            raise TransportError(f"injected failure for {path}")
        try:
            return self.responses[path]
        except KeyError:
            raise MarketDataFormatError(f"no stub response for {path}") from None


def depth_snapshot_payload(*, last_update_id: int, bids=((100.0, 3.0),), asks=((101.0, 2.0),), ts: int = BASE_TS) -> dict:
    return {
        "lastUpdateId": last_update_id,
        "E": ts,
        "T": ts,
        "bids": [[str(price), str(size)] for price, size in bids],
        "asks": [[str(price), str(size)] for price, size in asks],
    }


def depth_update_message(
    *,
    first_update_id: int,
    last_update_id: int,
    symbol: str = BINANCE_SYMBOL,
    bids=((100.0, 3.0),),
    asks=(),
    exchange_ts: int = BASE_TS,
    speed: str = "",
    previous_update_id: int | None = None,
) -> str:
    payload: dict[str, object] = {
        "e": "depthUpdate",
        "E": exchange_ts,
        "T": exchange_ts,
        "s": symbol,
        "U": first_update_id,
        "u": last_update_id,
        "b": [[str(price), str(size)] for price, size in bids],
        "a": [[str(price), str(size)] for price, size in asks],
    }
    if previous_update_id is not None:
        payload["pu"] = previous_update_id
    return json.dumps({"stream": depth_stream(symbol, speed=speed), "data": payload})


def agg_trade_message(
    *,
    aggregate_trade_id: int,
    price: float = 100.0,
    quantity: float = 1.0,
    is_buyer_maker: bool = True,
    symbol: str = BINANCE_SYMBOL,
    trade_ts: int = BASE_TS,
) -> str:
    payload = {
        "e": "aggTrade",
        "E": trade_ts,
        "a": aggregate_trade_id,
        "s": symbol,
        "p": str(price),
        "q": str(quantity),
        "f": aggregate_trade_id,
        "l": aggregate_trade_id,
        "T": trade_ts,
        "m": is_buyer_maker,
    }
    return json.dumps({"stream": f"{symbol.lower()}@aggTrade", "data": payload})


def mark_price_message(
    *, price: float = 100.5, symbol: str = BINANCE_SYMBOL, exchange_ts: int = BASE_TS
) -> str:
    payload = {"e": "markPriceUpdate", "E": exchange_ts, "s": symbol, "p": str(price)}
    return json.dumps({"stream": f"{symbol.lower()}@markPrice@1s", "data": payload})


def exchange_info_payload(
    *,
    symbol: str = BINANCE_SYMBOL,
    status: str = "TRADING",
    tick_size: str = "0.10",
    step_size: str = "0.001",
    min_notional: str = "5",
) -> dict:
    return {
        "timezone": "UTC",
        "serverTime": BASE_TS,
        "symbols": [
            {
                "symbol": symbol,
                "status": status,
                "pricePrecision": 2,
                "quantityPrecision": 3,
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": tick_size, "minPrice": "0.10", "maxPrice": "1000000"},
                    {"filterType": "LOT_SIZE", "stepSize": step_size, "minQty": "0.001", "maxQty": "1000"},
                    {"filterType": "MIN_NOTIONAL", "notional": min_notional},
                ],
            }
        ],
    }


__all__ = [
    "BINANCE_SYMBOL",
    "FakeConnection",
    "FakeHttp",
    "ScriptedTransport",
    "agg_trade_message",
    "depth_snapshot_payload",
    "depth_update_message",
    "exchange_info_payload",
    "live_config",
    "mark_price_message",
]
