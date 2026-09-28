"""外部成交事实归一化（P0001.9.3 §1）：Binance `userTrades` → `ExternalFill`。

注意：`/fapi/v1/userTrades` **只给 `orderId`**，没有 `clientOrderId` ⇒ 必须由调用方提供
`orderId → clientOrderId` 映射（来自 `allOrders`）。无法解析归属的成交**不会被当成我们的**：
计入 `unresolved_order_ids` 并跳过（不伪造、不猜测）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from execution.types import ExternalFill
from market.events.types import Venue

from connectors.binance.market_data.parsing import (
    require_decimal,
    require_field,
    require_int,
    require_mapping,
    require_optional_str,
    require_str,
)
from connectors.binance.private.errors import translate_market_errors, PrivateFormatError
from connectors.binance.private.orders import is_probex_order


@dataclass(frozen=True, slots=True)
class ExternalFillFacts:
    """归一化成交 + 不可归属统计。"""

    fills: tuple[ExternalFill, ...]
    #: 无法解析出（Probex）clientOrderId 的成交条数（审计用；这些成交不进入 reconcile）
    unresolved_order_ids: int


@translate_market_errors
def parse_external_fills(
    raw: object,
    *,
    symbol: str,
    order_id_to_client_id: Mapping[int, str],
    venue: Venue = Venue.BINANCE,
    fee_asset: str = "USDT",
) -> ExternalFillFacts:
    """解析 `userTrades` 响应；只保留能归属到 Probex 订单的成交。"""
    if not isinstance(symbol, str) or not symbol:
        raise PrivateFormatError("symbol must be a non-empty string")
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise PrivateFormatError("userTrades response must be an array")
    fills: list[ExternalFill] = []
    unresolved = 0
    for entry in raw:
        item = require_mapping(entry, path="userTrades[]")
        trade_symbol = require_str(require_field(item, "symbol", path="userTrades[]"), path="userTrades[].symbol")
        if trade_symbol != symbol:
            raise PrivateFormatError(f"userTrades[].symbol: expected {symbol!r}, got {trade_symbol!r}")
        order_id = require_int(require_field(item, "orderId", path="userTrades[]"), path="userTrades[].orderId")
        client_order_id = order_id_to_client_id.get(order_id)
        if client_order_id is None or not is_probex_order(client_order_id):
            unresolved += 1
            continue
        asset = require_optional_str(item.get("commissionAsset"), path="userTrades[].commissionAsset") or fee_asset
        if asset != fee_asset:
            raise PrivateFormatError(
                f"userTrades[].commissionAsset: {asset!r} != settlement asset {fee_asset!r} "
                "(不做多币种换算)"
            )
        fills.append(
            ExternalFill(
                client_order_id=client_order_id,
                execution_id=f"binance-trade-{require_int(require_field(item, 'id', path='userTrades[]'), path='userTrades[].id')}",
                trade_id=str(require_int(require_field(item, "id", path="userTrades[]"), path="userTrades[].id")),
                price=require_decimal(require_field(item, "price", path="userTrades[]"), path="userTrades[].price"),
                quantity=require_decimal(require_field(item, "qty", path="userTrades[]"), path="userTrades[].qty"),
                timestamp=require_int(require_field(item, "time", path="userTrades[]"), path="userTrades[].time"),
                fee=require_decimal(require_field(item, "commission", path="userTrades[]"), path="userTrades[].commission"),
                fee_asset=asset,
            )
        )
    return ExternalFillFacts(fills=tuple(fills), unresolved_order_ids=unresolved)


__all__ = ["ExternalFillFacts", "parse_external_fills"]
