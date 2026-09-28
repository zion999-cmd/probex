"""Binance `markPrice` → 独立事实 `MarkPriceObservation`（P0001.9.1 §0.3）。

mark price 必须作为**独立事实**处理：不得由 last trade 推导或替代，也不写入
`MarketEvent` / `market-state-v1`（本阶段只建立输入链，消费属 P0001.9.2/9.3）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from market.events.types import Milliseconds, Venue

from connectors.binance.market_data.errors import MarketDataFormatError
from connectors.binance.market_data.parsing import (
    require_decimal,
    require_field,
    require_int,
    require_mapping,
    require_str,
)

#: `markPrice` 报文的事件名。
MARK_PRICE_EVENT = "markPriceUpdate"


@dataclass(frozen=True, slots=True)
class MarkPriceObservation:
    """一次 mark price 观测（不可变外部事实）。"""

    venue: Venue
    symbol: str
    price: float
    #: 交易所事件时间（mark 自己的时间，不是 trade 时间）。
    exchange_ts: Milliseconds
    receive_ts: Milliseconds
    process_ts: Milliseconds

    def __post_init__(self) -> None:
        if not isinstance(self.venue, Venue):
            raise MarketDataFormatError(f"MarkPriceObservation.venue must be a Venue, got {type(self.venue).__name__}")
        if not isinstance(self.symbol, str) or not self.symbol:
            raise MarketDataFormatError("MarkPriceObservation.symbol must be a non-empty string")
        if isinstance(self.price, bool) or not isinstance(self.price, (int, float)):
            raise MarketDataFormatError("MarkPriceObservation.price must be a number")
        price = float(self.price)
        if not math.isfinite(price) or price <= 0.0:
            raise MarketDataFormatError(f"MarkPriceObservation.price must be positive and finite, got {self.price!r}")
        object.__setattr__(self, "price", price)
        for field in ("exchange_ts", "receive_ts", "process_ts"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise MarketDataFormatError(
                    f"MarkPriceObservation.{field} must be a non-negative int epoch-millisecond value"
                )


def parse_mark_price(
    raw: object,
    *,
    receive_ts: Milliseconds,
    process_ts: Milliseconds,
    symbol: str | None = None,
) -> MarkPriceObservation:
    """解析 WS `<symbol>@markPrice` 报文（字段 `p` = mark price，`E` = 事件时间）。"""
    path = "markPrice"
    message = require_mapping(raw, path=path)
    event_name = require_str(require_field(message, "e", path=path), path=f"{path}.e")
    if event_name != MARK_PRICE_EVENT:
        raise MarketDataFormatError(f"{path}.e: expected {MARK_PRICE_EVENT!r}, got {event_name!r}")
    message_symbol = require_str(require_field(message, "s", path=path), path=f"{path}.s")
    if symbol is not None and message_symbol != symbol:
        raise MarketDataFormatError(f"{path}.s: expected {symbol!r}, got {message_symbol!r}")
    price = require_decimal(require_field(message, "p", path=path), path=f"{path}.p")
    exchange_ts = require_int(require_field(message, "E", path=path), path=f"{path}.E")

    return MarkPriceObservation(
        venue=Venue.BINANCE,
        symbol=message_symbol,
        price=price,
        exchange_ts=exchange_ts,
        receive_ts=receive_ts,
        process_ts=process_ts,
    )


__all__ = ["MARK_PRICE_EVENT", "MarkPriceObservation", "parse_mark_price"]
