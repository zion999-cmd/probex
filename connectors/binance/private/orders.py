"""外部订单事实归一化（P0001.9.3 §1）：Binance → `ExternalOrder`（**不另造订单模型**）。

纪律：

- 只把 `clientOrderId` 以 `probex-` 开头的订单视为**本系统自己的**；
  非 Probex 的**未平挂单**必须由调用方判定为 `FOREIGN_OPEN_ORDER` → Recovery BLOCKED（不忽略、不撤、不 adopt）。
- 严格解析：缺字段 / 未知状态 / 非有限数值一律 `PrivateFormatError`（fail closed）。
- 状态映射显式列出（含 `EXPIRED_IN_MATCH` 等），不猜。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from execution.tracker import CLIENT_ORDER_ID_PREFIX
from execution.types import ExternalOrder, OrderStatus
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

#: Binance 订单状态 → 本地 `OrderStatus`（显式映射，未知即报错）。
_STATUS_MAP: dict[str, OrderStatus] = {
    "NEW": OrderStatus.OPEN,
    "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
    "FILLED": OrderStatus.FILLED,
    "CANCELED": OrderStatus.CANCELED,
    "EXPIRED": OrderStatus.EXPIRED,
    "EXPIRED_IN_MATCH": OrderStatus.EXPIRED,
    "REJECTED": OrderStatus.FAILED,
}


def is_probex_order(client_order_id: str) -> bool:
    """ownership boundary：只有 `probex-` 前缀的订单属于本系统。"""
    return isinstance(client_order_id, str) and client_order_id.startswith(CLIENT_ORDER_ID_PREFIX)


@dataclass(frozen=True, slots=True)
class ExternalOrderFacts:
    """归一化结果 + ownership 分类（`recovery.py` 据此外决定 BLOCKED / reconcile）。"""

    #: Probex 自己的**当前挂单**
    open_orders: tuple[ExternalOrder, ...]
    #: Probex 自己的**历史订单**（每个 clientOrderId 只保留最新一条）
    history_orders: tuple[ExternalOrder, ...]
    #: 非 Probex 的**未平挂单**（存在即 → RECOVERY BLOCKED: FOREIGN_OPEN_ORDER）
    foreign_open_orders: tuple[ExternalOrder, ...]
    #: 被丢弃的非 Probex 历史订单/成交数量（审计用；不参与 reconcile）
    foreign_ignored: int


@translate_market_errors
def parse_external_orders(
    raw: object, *, symbol: str, venue: Venue = Venue.BINANCE
) -> tuple[ExternalOrder, ...]:
    """解析 `openOrders` / `allOrders` 响应为 `ExternalOrder` 元组（保持输入顺序）。"""
    if not isinstance(symbol, str) or not symbol:
        raise PrivateFormatError("symbol must be a non-empty string")
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise PrivateFormatError("orders response must be an array")
    orders: list[ExternalOrder] = []
    for entry in raw:
        item = require_mapping(entry, path="orders[]")
        orders.append(_parse_order(item, symbol=symbol, venue=venue))
    return tuple(orders)


def classify_orders(
    *, open_orders: tuple[ExternalOrder, ...], history: tuple[ExternalOrder, ...]
) -> ExternalOrderFacts:
    """按 ownership boundary 分类：Probex 订单进入 reconcile；非 Probex 未平挂单触发 BLOCKED。"""
    probex_open: list[ExternalOrder] = []
    probex_history: list[ExternalOrder] = []
    foreign_open: list[ExternalOrder] = []
    foreign_ignored = 0
    for order in open_orders:
        if is_probex_order(order.client_order_id):
            probex_open.append(order)
        else:
            foreign_open.append(order)
    for order in history:
        if is_probex_order(order.client_order_id):
            probex_history.append(order)
        else:
            foreign_ignored += 1
    return ExternalOrderFacts(
        open_orders=tuple(probex_open),
        history_orders=tuple(probex_history),
        foreign_open_orders=tuple(foreign_open),
        foreign_ignored=foreign_ignored,
    )


def latest_by_client_order_id(orders: tuple[ExternalOrder, ...]) -> dict[str, ExternalOrder]:
    """同一 clientOrderId 只保留**最后出现**的一条。

    `ExternalOrder`（既有契约）不含时间戳字段，因此本函数要求输入**已按时间升序**；
    `recovery.fetch_snapshot()` 会在解析前按 Binance 的 `updateTime` 稳定排序，保证这一点。
    """
    latest: dict[str, ExternalOrder] = {}
    for order in orders:
        latest[order.client_order_id] = order
    return latest


def sort_orders_by_time(entries: "Sequence[Mapping[str, object]]") -> list[Mapping[str, object]]:
    """按 Binance 的 `updateTime`/`time` 稳定升序排序（缺失时间戳时保持原顺序）。"""
    indexed = list(enumerate(entries))
    indexed.sort(key=lambda item: (order_update_timestamp(item[1]), item[0]))
    return [entry for _index, entry in indexed]


def _parse_order(entry: Mapping[str, object], *, symbol: str, venue: Venue) -> ExternalOrder:
    path = "orders[]"
    order_symbol = require_str(require_field(entry, "symbol", path=path), path=f"{path}.symbol")
    if order_symbol != symbol:
        raise PrivateFormatError(f"{path}.symbol: expected {symbol!r}, got {order_symbol!r}")
    client_order_id = require_str(require_field(entry, "clientOrderId", path=path), path=f"{path}.clientOrderId")
    status_text = require_str(require_field(entry, "status", path=path), path=f"{path}.status")
    try:
        status = _STATUS_MAP[status_text]
    except KeyError:
        raise PrivateFormatError(f"{path}.status: unsupported Binance order status {status_text!r}") from None
    filled = require_decimal(require_field(entry, "executedQty", path=path), path=f"{path}.executedQty")
    avg_price = require_decimal(require_field(entry, "avgPrice", path=path), path=f"{path}.avgPrice")
    if filled > 0.0 and avg_price <= 0.0:
        raise PrivateFormatError(f"{path}: executedQty>0 requires avgPrice>0 (fail closed)")
    return ExternalOrder(
        client_order_id=client_order_id,
        exchange_order_id=str(require_int(require_field(entry, "orderId", path=path), path=f"{path}.orderId")),
        symbol=order_symbol,
        status=status,
        filled_quantity=filled,
        avg_fill_price=avg_price,
        side=_parse_side(require_str(require_field(entry, "side", path=path), path=f"{path}.side")),
        quantity=require_decimal(require_field(entry, "origQty", path=path), path=f"{path}.origQty"),
        price=require_decimal(require_field(entry, "price", path=path), path=f"{path}.price"),
    )


def _parse_side(value: str) -> "Side":  # noqa: F821 - 见下方 import
    from portfolio.types import Side

    try:
        return Side(value.lower())
    except ValueError:
        raise PrivateFormatError(f"orders[].side: unsupported side {value!r}") from None


def order_update_timestamp(entry: Mapping[str, object]) -> int:
    """订单记录的时间戳（`updateTime` 优先，回退 `time`）——用于取「最新一条」。"""
    for name in ("updateTime", "time"):
        value = entry.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    raise PrivateFormatError("orders[]: missing updateTime/time")


__all__ = [
    "ExternalOrderFacts",
    "classify_orders",
    "is_probex_order",
    "latest_by_client_order_id",
    "order_update_timestamp",
    "parse_external_orders",
]
