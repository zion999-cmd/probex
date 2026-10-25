"""Bar OHLCV 回测的成交适配器（人类 2026-10-24 授权双模式成交假设）。

实现 `ExecutionAdapter` 契约，由 bar 回测驱动器在每根 bar 收盘后调用 `on_bar()`。
两种显式成交模式（不发明盘内路径）：

1. **limit_conservative（保守限价）**
   - 挂单在**下一根** bar 才评估（当根决策不看未来，杜绝前视）；
   - BUY 成交 ⇔ bar.low ≤ 限价；SELL 成交 ⇔ bar.high ≥ 限价；成交价 = 限价（maker 费率）；
   - 限价单成交价固定，盘内 high/low 先后不影响成交价格（无队列、无部分成交推断）；
   - 一张挂单最多成交一次，未触碰则保留到后续 bar。

2. **close_market（收盘市价）**
   - bar n 收盘决策 ⇒ 非 post-only 订单在 **bar n+1 的 open** 成交；
   - 价格 = open 经显式 `FixedBpsSlippage` 调整（买高卖低），taker 费率；
   - 必须显式配置滑点与 taker 费率，否则 fail-closed（不默认零滑点）。

盘口事实只有 OHLCV；成交量不参与成交推断（不假设可吃深度）。
"""

from __future__ import annotations

from market.events.payloads import BarPayload
from market.events.types import Milliseconds, Venue
from execution.events import (
    FillReceived,
    OrderAccepted,
    OrderCanceled,
    OrderRejected,
)
from execution.simulation.fees import FeeSchedule
from execution.simulation.slippage import FixedBpsSlippage
from execution.types import ExternalFill, ExternalOrder, Order, OrderStatus
from portfolio.types import Side

LIMIT_CONSERVATIVE = "limit_conservative"
CLOSE_MARKET = "close_market"
BAR_EXECUTION_MODES = (LIMIT_CONSERVATIVE, CLOSE_MARKET)


class BarAdapterError(ValueError):
    """bar 适配器契约错误。"""


class _RestingBarOrder:
    __slots__ = ("client_order_id", "exchange_order_id", "symbol", "side", "price",
                 "quantity", "post_only", "status", "created_bar", "filled_quantity",
                 "avg_fill_price", "cancel_requested")

    def __init__(self, order: Order, exchange_order_id: str, created_bar: int) -> None:
        self.client_order_id = order.client_order_id
        self.exchange_order_id = exchange_order_id
        self.symbol = order.symbol
        self.side = order.side
        self.price = order.price
        self.quantity = order.quantity
        self.post_only = order.post_only
        self.status = OrderStatus.OPEN
        self.created_bar = created_bar
        self.filled_quantity = 0.0
        self.avg_fill_price = 0.0
        self.cancel_requested = False

    def is_terminal(self) -> bool:
        return self.status in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.FAILED)


class BarExecutionAdapter:
    """OHLCV bar 上的确定性成交适配器（单 symbol；无队列、无部分成交推断）。"""

    def __init__(
        self,
        *,
        symbol: str,
        mode: str,
        fee_schedule: FeeSchedule,
        slippage: FixedBpsSlippage | None = None,
        venue: Venue = Venue.BINANCE,
        exchange_id_prefix: str = "bar",
    ) -> None:
        if mode not in BAR_EXECUTION_MODES:
            raise BarAdapterError(f"unknown bar execution mode {mode!r}")
        if not isinstance(fee_schedule, FeeSchedule):
            raise BarAdapterError("fee_schedule must be a FeeSchedule")
        if mode == CLOSE_MARKET and slippage is None:
            raise BarAdapterError("close_market mode requires an explicit FixedBpsSlippage")
        if mode == CLOSE_MARKET and fee_schedule.taker_fee_rate is None:
            raise BarAdapterError("close_market mode requires taker_fee_rate")
        self._symbol = symbol
        self._mode = mode
        self._fees = fee_schedule
        self._slippage = slippage
        self._venue = venue
        self._prefix = exchange_id_prefix
        self._orders: dict[str, _RestingBarOrder] = {}
        self._outbox: list = []
        self._fills: list[ExternalFill] = []
        self._bar_index = -1
        self._next_id = 0

    # ------------------------------------------------------------------ ExecutionAdapter

    def submit(self, order: Order) -> tuple:
        if order.client_order_id in self._orders:
            return (OrderRejected(order.client_order_id, "DUPLICATE_CLIENT_ORDER_ID",
                                     order.created_at),)
        if self._mode == CLOSE_MARKET and order.post_only:
            return (OrderRejected(order.client_order_id, "POST_ONLY_UNSUPPORTED_IN_CLOSE_MARKET",
                                     order.created_at),)
        if self._mode == LIMIT_CONSERVATIVE and not order.post_only:
            return (OrderRejected(order.client_order_id, "TAKER_NOT_SUPPORTED",
                                     order.created_at),)
        self._next_id += 1
        exchange_order_id = f"{self._prefix}-{self._next_id:06d}"
        resting = _RestingBarOrder(order, exchange_order_id, self._bar_index)
        self._orders[order.client_order_id] = resting
        return (OrderAccepted(order.client_order_id, exchange_order_id, order.created_at),)

    def cancel(self, order: Order) -> tuple:
        resting = self._orders.get(order.client_order_id)
        if resting is None or resting.is_terminal():
            return (OrderRejected(order.client_order_id, "CANCEL_UNKNOWN_ORDER",
                                     order.updated_at),)
        resting.status = OrderStatus.CANCELED
        resting.cancel_requested = True
        return (OrderCanceled(resting.client_order_id, order.updated_at),)

    def poll(self) -> tuple:
        events = tuple(self._outbox)
        self._outbox.clear()
        return events

    def open_orders(self) -> tuple[ExternalOrder, ...]:
        return tuple(
            ExternalOrder(
                client_order_id=o.client_order_id, exchange_order_id=o.exchange_order_id,
                symbol=o.symbol, status=o.status, filled_quantity=o.filled_quantity,
                avg_fill_price=o.avg_fill_price, side=o.side, quantity=o.quantity)
            for o in self._orders.values() if not o.is_terminal())

    def recent_fills(self, *, since_ms: Milliseconds | None = None) -> tuple[ExternalFill, ...]:
        if since_ms is None:
            return tuple(self._fills)
        return tuple(f for f in self._fills if f.timestamp >= since_ms)

    # ------------------------------------------------------------------ bar 步进

    def on_bar(self, bar: BarPayload) -> None:
        """处理一根**已收盘**的 bar：评估上一根挂单 / 市价单的成交。"""
        self._bar_index += 1
        for resting in list(self._orders.values()):
            if resting.is_terminal() or resting.created_bar >= self._bar_index:
                continue
            if self._mode == LIMIT_CONSERVATIVE:
                self._evaluate_limit(resting, bar)
            else:
                self._evaluate_market(resting, bar)

    def _evaluate_limit(self, order: _RestingBarOrder, bar: BarPayload) -> None:
        # 限价固定成交价，盘内路径不影响成交价格；保守性体现在"仅下一根 bar 可成交"。
        touched = bar.low <= order.price if order.side is Side.BUY else bar.high >= order.price
        if not touched:
            return
        self._fill(order, price=order.price, ts=bar.close_ts, is_maker=True,
                     fee_rate=self._fees.maker_fee_rate)

    def _evaluate_market(self, order: _RestingBarOrder, bar: BarPayload) -> None:
        assert self._slippage is not None
        price = self._slippage.exec_price(side=order.side, reference_price=bar.open)
        self._fill(order, price=price, ts=bar.open_ts, is_maker=False,
                     fee_rate=float(self._fees.taker_fee_rate))

    def _fill(self, order: _RestingBarOrder, *, price: float, ts: Milliseconds,
                is_maker: bool, fee_rate: float) -> None:
        fee = price * order.quantity * fee_rate
        self._next_id += 1
        execution_id = f"{self._prefix}-exec-{self._next_id:06d}"
        trade_id = f"{self._prefix}-trade-{self._next_id:06d}"
        order.filled_quantity = order.quantity
        order.avg_fill_price = price
        order.status = OrderStatus.FILLED
        self._fills.append(ExternalFill(
            client_order_id=order.client_order_id, execution_id=execution_id,
            trade_id=trade_id, price=price, quantity=order.quantity, timestamp=ts,
            fee=fee, fee_asset=self._fees.fee_asset))
        self._outbox.append(FillReceived(
            client_order_id=order.client_order_id, execution_id=execution_id,
            trade_id=trade_id, price=price, quantity=order.quantity, timestamp=ts,
            fee=fee, fee_asset=self._fees.fee_asset, is_maker=is_maker))


__all__ = [
    "BAR_EXECUTION_MODES",
    "CLOSE_MARKET",
    "LIMIT_CONSERVATIVE",
    "BarAdapterError",
    "BarExecutionAdapter",
]
