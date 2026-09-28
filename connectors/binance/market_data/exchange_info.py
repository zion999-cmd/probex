"""Binance `exchangeInfo` → `TradingRules`（P0001.9.1 §0.5）。

纪律：**只用 `filters`**。`pricePrecision` / `quantityPrecision` 是展示用字段，
不得用来推导 tick / step / 边界；filter 缺失即报错（fail closed，绝不猜）。
P0001.9.2 的下单校验直接消费本类型。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

from connectors.binance.market_data.errors import MarketDataFormatError
from connectors.binance.market_data.parsing import (
    require_decimal,
    require_field,
    require_mapping,
    require_str,
)

_PRICE_FILTER = "PRICE_FILTER"
_LOT_SIZE = "LOT_SIZE"
_MIN_NOTIONAL = "MIN_NOTIONAL"


@dataclass(frozen=True, slots=True)
class TradingRules:
    """某个 symbol 的可交易规则（全部来自 `filters`）。"""

    symbol: str
    status: str
    tick_size: float
    min_price: float
    max_price: float
    step_size: float
    min_qty: float
    max_qty: float
    min_notional: float

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol:
            raise MarketDataFormatError("TradingRules.symbol must be a non-empty string")
        if not isinstance(self.status, str) or not self.status:
            raise MarketDataFormatError("TradingRules.status must be a non-empty string")
        for field in ("tick_size", "min_price", "max_price", "step_size", "min_qty", "max_qty", "min_notional"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise MarketDataFormatError(f"TradingRules.{field} must be a finite number")
            object.__setattr__(self, field, float(value))
        for field in ("tick_size", "step_size", "min_qty"):
            if getattr(self, field) <= 0.0:
                raise MarketDataFormatError(f"TradingRules.{field} must be > 0")
        if self.min_price <= 0.0:
            raise MarketDataFormatError("TradingRules.min_price must be > 0")
        if self.max_price < self.min_price:
            raise MarketDataFormatError("TradingRules.max_price must be >= min_price")
        if self.max_qty < self.min_qty:
            raise MarketDataFormatError("TradingRules.max_qty must be >= min_qty")
        if self.min_notional < 0.0:
            raise MarketDataFormatError("TradingRules.min_notional must be >= 0")

    @property
    def is_trading(self) -> bool:
        """该 symbol 当前是否处于可交易状态（`status == "TRADING"`）。"""
        return self.status == "TRADING"


def parse_exchange_info(raw: object, *, symbol: str) -> TradingRules:
    """从 `GET /fapi/v1/exchangeInfo` 响应中提取指定 symbol 的规则。"""
    path = "exchangeInfo"
    if not isinstance(symbol, str) or not symbol:
        raise MarketDataFormatError("symbol must be a non-empty string")
    message = require_mapping(raw, path=path)
    symbols = require_field(message, "symbols", path=path)
    if isinstance(symbols, (str, bytes)) or not isinstance(symbols, Sequence):
        raise MarketDataFormatError(f"{path}.symbols: expected an array")
    for entry in symbols:
        item = require_mapping(entry, path=f"{path}.symbols[]")
        if item.get("symbol") == symbol:
            return _rules_from_symbol(item, symbol=symbol)
    raise MarketDataFormatError(f"{path}.symbols: symbol {symbol!r} not found")


def _rules_from_symbol(item: Mapping[str, object], *, symbol: str) -> TradingRules:
    status = require_str(require_field(item, "status", path=f"exchangeInfo[{symbol}]"), path="exchangeInfo.status")
    filters = _filter_map(item, symbol=symbol)
    price = _require_filter(filters, _PRICE_FILTER, symbol=symbol)
    lot = _require_filter(filters, _LOT_SIZE, symbol=symbol)
    notional = _require_filter(filters, _MIN_NOTIONAL, symbol=symbol)
    return TradingRules(
        symbol=symbol,
        status=status,
        tick_size=require_decimal(require_field(price, "tickSize", path="PRICE_FILTER"), path="PRICE_FILTER.tickSize"),
        min_price=require_decimal(require_field(price, "minPrice", path="PRICE_FILTER"), path="PRICE_FILTER.minPrice"),
        max_price=require_decimal(require_field(price, "maxPrice", path="PRICE_FILTER"), path="PRICE_FILTER.maxPrice"),
        step_size=require_decimal(require_field(lot, "stepSize", path="LOT_SIZE"), path="LOT_SIZE.stepSize"),
        min_qty=require_decimal(require_field(lot, "minQty", path="LOT_SIZE"), path="LOT_SIZE.minQty"),
        max_qty=require_decimal(require_field(lot, "maxQty", path="LOT_SIZE"), path="LOT_SIZE.maxQty"),
        min_notional=require_decimal(
            require_field(notional, "notional", path="MIN_NOTIONAL"), path="MIN_NOTIONAL.notional"
        ),
    )


def _filter_map(item: Mapping[str, object], *, symbol: str) -> dict[str, Mapping[str, object]]:
    raw_filters = require_field(item, "filters", path=f"exchangeInfo[{symbol}]")
    if isinstance(raw_filters, (str, bytes)) or not isinstance(raw_filters, Sequence):
        raise MarketDataFormatError(f"exchangeInfo[{symbol}].filters: expected an array")
    result: dict[str, Mapping[str, object]] = {}
    for entry in raw_filters:
        filter_item = require_mapping(entry, path=f"exchangeInfo[{symbol}].filters[]")
        filter_type = require_str(
            require_field(filter_item, "filterType", path=f"exchangeInfo[{symbol}].filters[].filterType"),
            path="filterType",
        )
        result[filter_type] = filter_item
    return result


def _require_filter(
    filters: Mapping[str, Mapping[str, object]], filter_type: str, *, symbol: str
) -> Mapping[str, object]:
    try:
        return filters[filter_type]
    except KeyError:
        raise MarketDataFormatError(
            f"exchangeInfo[{symbol}]: missing {filter_type} filter (refusing to infer limits from precision fields)"
        ) from None


__all__ = ["TradingRules", "parse_exchange_info"]
