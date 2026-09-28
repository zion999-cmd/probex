"""Binance `aggTrade` → canonical `TradePayload`（P0001.9.1 §0.3）。

映射纪律（不得反转）：Binance `m` = `isBuyerMaker`，即**买方是挂单方** ⇒ 成交的
主动方（aggressor）是 **SELL**；`m = false` ⇒ aggressor 为 **BUY**。

去重纪律：同一个 `aggregate_trade_id` 只允许被消费者看到一次 —— 用**单调水位**实现
（内存有界），水位之下（重复 / 重放 / 乱序旧值）一律丢弃并计数，绝不静默重复投递。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from market.events.payloads import AggressorSide, TradePayload
from market.events.types import EventType, MarketEvent, Milliseconds, Venue

from connectors.binance.market_data.errors import MarketDataFormatError
from connectors.binance.market_data.parsing import (
    require_bool,
    require_decimal,
    require_field,
    require_int,
    require_mapping,
    require_str,
)

#: `aggTrade` 报文的事件名。
AGG_TRADE_EVENT = "aggTrade"

_MIN_BUYER_MAKER_PATH = "aggTrade.m"


def parse_agg_trade(
    raw: object,
    *,
    receive_ts: Milliseconds,
    process_ts: Milliseconds,
    symbol: str | None = None,
) -> MarketEvent:
    """解析 WS `<symbol>@aggTrade` 报文。

    `symbol` 给定时必须与报文一致（防止把别的 symbol 的事件喂进本通道）。
    `exchange_ts` 取交易时间 `T`（不是事件时间 `E`）。
    """
    path = "aggTrade"
    message = require_mapping(raw, path=path)
    event_name = require_str(require_field(message, "e", path=path), path=f"{path}.e")
    if event_name != AGG_TRADE_EVENT:
        raise MarketDataFormatError(f"{path}.e: expected {AGG_TRADE_EVENT!r}, got {event_name!r}")
    message_symbol = require_str(require_field(message, "s", path=path), path=f"{path}.s")
    if symbol is not None and message_symbol != symbol:
        raise MarketDataFormatError(f"{path}.s: expected {symbol!r}, got {message_symbol!r}")

    aggregate_trade_id = require_int(require_field(message, "a", path=path), path=f"{path}.a")
    price = require_decimal(require_field(message, "p", path=path), path=f"{path}.p")
    quantity = require_decimal(require_field(message, "q", path=path), path=f"{path}.q")
    trade_ts = require_int(require_field(message, "T", path=path), path=f"{path}.T")
    is_buyer_maker = require_bool(require_field(message, "m", path=path), path=_MIN_BUYER_MAKER_PATH)

    aggressor = AggressorSide.SELL if is_buyer_maker else AggressorSide.BUY
    try:
        payload = TradePayload(
            aggregate_trade_id=aggregate_trade_id,
            price=price,
            quantity=quantity,
            aggressor=aggressor,
        )
        return MarketEvent(
            venue=Venue.BINANCE,
            symbol=message_symbol,
            event_type=EventType.TRADE,
            exchange_ts=trade_ts,
            receive_ts=receive_ts,
            process_ts=process_ts,
            sequence=None,
            payload=payload,
        )
    except Exception as exc:  # payload / event 不变量失败 → 边界错误
        raise MarketDataFormatError(f"{path}: {exc}") from exc


def aggressor_for_buyer_maker(is_buyer_maker: bool) -> AggressorSide:
    """`isBuyerMaker` → aggressor（独立函数以便单独测试语义）。"""
    if not isinstance(is_buyer_maker, bool):
        raise MarketDataFormatError("aggTrade.m: expected a boolean")
    return AggressorSide.SELL if is_buyer_maker else AggressorSide.BUY


@dataclass
class AggTradeDeduplicator:
    """按 symbol 的单调水位去重（有界内存）。"""

    _watermark: dict[str, int] = field(default_factory=dict)
    dropped: int = 0

    def accept(self, *, symbol: str, aggregate_trade_id: int) -> bool:
        """是否应当把该成交投递给消费者（id 必须严格大于水位）。"""
        if not isinstance(symbol, str) or not symbol:
            raise MarketDataFormatError("symbol must be a non-empty string")
        if isinstance(aggregate_trade_id, bool) or not isinstance(aggregate_trade_id, int):
            raise MarketDataFormatError("aggregate_trade_id must be an int")
        watermark = self._watermark.get(symbol)
        if watermark is not None and aggregate_trade_id <= watermark:
            self.dropped += 1
            return False
        self._watermark[symbol] = aggregate_trade_id
        return True

    def watermark(self, symbol: str) -> int | None:
        return self._watermark.get(symbol)


__all__ = ["AGG_TRADE_EVENT", "AggTradeDeduplicator", "aggressor_for_buyer_maker", "parse_agg_trade"]
