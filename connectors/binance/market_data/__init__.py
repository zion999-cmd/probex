"""Binance 市场数据归一化模块。"""

from __future__ import annotations

from connectors.binance.market_data.depth import (
    BINANCE_VENUE,
    DEPTH_UPDATE_EVENT,
    parse_depth_diff,
    parse_depth_snapshot,
)
from connectors.binance.market_data.endpoints import (
    DEPTH_PATH,
    EXCHANGE_INFO_PATH,
    REST_BASE_URL,
    SERVER_TIME_PATH,
    WS_HOST,
    StreamTier,
)
from connectors.binance.market_data.errors import (
    MarketDataFormatError,
    ReconnectExhaustedError,
    TransportError,
    WebSocketClosed,
    WebSocketHandshakeError,
    WebSocketProtocolError,
    WebSocketTimeout,
)
from connectors.binance.market_data.exchange_info import TradingRules, parse_exchange_info
from connectors.binance.market_data.mark import MarkPriceObservation, parse_mark_price
from connectors.binance.market_data.runtime import (
    MarketDataBatch,
    LiveMarketDataConfig,
    LiveMarketDataRuntime,
    LiveMarketDataTelemetry,
    RunSummary,
)
from connectors.binance.market_data.snapshot import (
    DepthSnapshotClient,
    ExchangeInfoClient,
    JsonHttpClient,
    ServerTimeClient,
    UrllibJsonClient,
    wall_clock_ms,
)
from connectors.binance.market_data.streams import (
    DEPTH_SPEED_100MS,
    MARK_PRICE_SPEED_1S,
    StreamKind,
    StreamMessage,
    agg_trade_stream,
    depth_stream,
    mark_price_stream,
    parse_message,
    stream_matches,
    streams_for,
    subscribe_message,
)
from connectors.binance.market_data.trades import (
    AGG_TRADE_EVENT,
    AggTradeDeduplicator,
    aggressor_for_buyer_maker,
    parse_agg_trade,
)
from connectors.binance.market_data.transport import ReconnectPolicy, WebSocketConnection, connect

__all__ = [
    "AGG_TRADE_EVENT",
    "AggTradeDeduplicator",
    "BINANCE_VENUE",
    "DEPTH_PATH",
    "DEPTH_UPDATE_EVENT",
    "DEPTH_SPEED_100MS",
    "DepthSnapshotClient",
    "ExchangeInfoClient",
    "EXCHANGE_INFO_PATH",
    "JsonHttpClient",
    "LiveMarketDataConfig",
    "LiveMarketDataRuntime",
    "LiveMarketDataTelemetry",
    "MARK_PRICE_SPEED_1S",
    "MarkPriceObservation",
    "MarketDataBatch",
    "MarketDataFormatError",
    "REST_BASE_URL",
    "ReconnectPolicy",
    "RunSummary",
    "ServerTimeClient",
    "TradingRules",
    "ReconnectExhaustedError",
    "SERVER_TIME_PATH",
    "StreamKind",
    "StreamMessage",
    "StreamTier",
    "TransportError",
    "UrllibJsonClient",
    "WS_HOST",
    "WebSocketClosed",
    "WebSocketHandshakeError",
    "WebSocketProtocolError",
    "WebSocketTimeout",
    "WebSocketConnection",
    "aggressor_for_buyer_maker",
    "agg_trade_stream",
    "connect",
    "depth_stream",
    "mark_price_stream",
    "parse_agg_trade",
    "parse_depth_diff",
    "parse_depth_snapshot",
    "parse_exchange_info",
    "parse_mark_price",
    "parse_message",
    "stream_matches",
    "streams_for",
    "subscribe_message",
    "wall_clock_ms",
]
