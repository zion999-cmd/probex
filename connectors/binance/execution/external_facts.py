"""`PrivateExternalFactsProvider`：`ExternalFactsProvider` 的**产品实现**（P0001.9.7 §6）。

把只读 private 层（`PrivateRestClient` + 既有 parser）接到 execution adapter 需要的两个方法上：

```text
parse_open_orders(symbol)   → /fapi/v1/openOrders  → ExternalOrder（只含 Probex 自己的）
parse_recent_fills(symbol)  → /fapi/v1/userTrades  → ExternalFill（经 orderId → clientOrderId 反查）
```

纪律（延续 P0001.9.6 / P0001.9.3.1）：

- **读取失败 ⇒ 抛错**（`ExternalFactsUnavailableError` 由 adapter 统一包装）——**绝不**降级成空集合；
- **交易所确认无挂单/无成交 ⇒ 空元组**；
- 归属不明的成交**不擅自认领**（交由 recovery/reconciliation 的完整窗口处理）；
- 不缓存、不推断、不做缓存式"最近已知"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from market.events.types import Milliseconds, Venue

from connectors.binance.execution.adapter import ExternalFactsUnavailableError
from connectors.binance.private.orders import is_probex_order, parse_external_orders
from connectors.binance.private.rest import PrivateRestClient
from connectors.binance.private.trades import parse_external_fills
from execution.types import ExternalFill, ExternalOrder

#: 用于解析成交归属的订单窗口上限（与 P0001.9.3 的事实窗口语义一致；显式参数，无业务默认值）。
DEFAULT_ORDER_WINDOW = 100


@dataclass
class PrivateExternalFactsProvider:
    """只读 private 层的 `ExternalFactsProvider` 实现。"""

    rest: PrivateRestClient
    order_window: int = DEFAULT_ORDER_WINDOW
    venue: Venue = Venue.BINANCE
    clock: Callable[[], Milliseconds] | None = None
    #: 上一次读取时的归属不明成交数（telemetry；**不影响**返回值语义）
    unresolved_fill_orders: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.rest, PrivateRestClient):
            raise ExternalFactsUnavailableError("PrivateExternalFactsProvider requires a PrivateRestClient")
        if isinstance(self.order_window, bool) or not isinstance(self.order_window, int) or self.order_window < 1:
            raise ExternalFactsUnavailableError("order_window must be an int >= 1")

    # ------------------------------------------------------------------ ExternalFactsProvider

    def parse_open_orders(self, symbol: str) -> tuple[ExternalOrder, ...]:
        """真实挂单；**读取/解析失败即抛错**（调用方据此按 UNKNOWN 处理）。"""
        try:
            raw = self.rest.open_orders(symbol)
            parsed = parse_external_orders(raw, symbol=symbol, venue=self.venue)
        except Exception as exc:  # noqa: BLE001 - 一切失败都必须显式不可用
            raise ExternalFactsUnavailableError(f"open orders read failed: {type(exc).__name__}") from exc
        return tuple(order for order in parsed if is_probex_order(order.client_order_id))

    def parse_recent_fills(
        self, symbol: str, *, since_ms: Milliseconds | None = None
    ) -> tuple[ExternalFill, ...]:
        """真实近期成交（经 orderId → clientOrderId 反查）；读取/解析失败即抛错。"""
        try:
            trades = self.rest.user_trades(symbol, limit=self.order_window)
            orders = parse_external_orders(
                self.rest.order_history(symbol, limit=self.order_window), symbol=symbol, venue=self.venue
            )
        except Exception as exc:  # noqa: BLE001
            raise ExternalFactsUnavailableError(f"recent fills read failed: {type(exc).__name__}") from exc
        order_id_to_client = {
            int(order.exchange_order_id): order.client_order_id
            for order in orders
            if order.exchange_order_id is not None
        }
        try:
            facts = parse_external_fills(
                trades,
                symbol=symbol,
                order_id_to_client_id=order_id_to_client,
                venue=self.venue,
                fee_asset="USDT",
            )
        except Exception as exc:  # noqa: BLE001
            raise ExternalFactsUnavailableError(f"recent fills parse failed: {type(exc).__name__}") from exc
        self.unresolved_fill_orders = len(facts.unresolved_order_ids)
        fills = facts.fills
        if since_ms is not None:
            fills = tuple(fill for fill in fills if fill.timestamp >= since_ms)
        return fills


__all__ = ["DEFAULT_ORDER_WINDOW", "PrivateExternalFactsProvider"]
