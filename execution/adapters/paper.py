"""PaperBroker：可控的 ExecutionAdapter（P0001.6 §12）。

 Paper 不是「submit → 立即成交」，而是由测试**显式驱动**的交易所替身：

```text
broker.submit(order)              → OrderAccepted | OrderRejected（可脚本化 reject / drop）
broker.cancel(order)              → OrderCanceled（默认立即确认；可脚本化 defer）
broker.fill(...)                  → 把 FillReceived 放进 outbox，由 poll() 取出
broker.cancel_ack(...)            → 把撤单确认放进 outbox（用于 cancel race）
broker.status_update(...)         → 把外部状态变化放进 outbox（用于 LOST / 恢复）
broker.drop_from_external(id)     → 从外部挂单里移除（用于「本地有、外部没有」）
```

本阶段**不做盘口撮合**（属 P0001.8），但实现基本 venue rule：post-only 穿价即拒绝。

注：`Order` 的不变量已保证 price / quantity 合法，因此本 adapter 不再重复校验数值合法性
（adapter 校验的是它能从外部收到的东西：重复 client_order_id、盘口规则）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from execution.events import (
    ExecutionEvent,
    FillReceived,
    OrderAccepted,
    OrderCanceled,
    OrderRejected,
    OrderStatusUpdate,
)
from execution.types import ExternalFill, ExternalOrder, Order, OrderStatus
from market.events.types import Milliseconds, Venue


class PaperRejectReason(Enum):
    """PaperBroker 的拒绝原因（模拟 venue rule）。"""

    DUPLICATE_CLIENT_ORDER_ID = "DUPLICATE_CLIENT_ORDER_ID"
    REJECT_POST_ONLY = "REJECT_POST_ONLY"
    CANCEL_UNKNOWN_ORDER = "CANCEL_UNKNOWN_ORDER"
    SCRIPTED_REJECT = "SCRIPTED_REJECT"


@dataclass
class _PaperOrder:
    """broker 侧订单状态。"""

    client_order_id: str
    exchange_order_id: str
    symbol: str
    side: object
    price: float
    quantity: float
    status: OrderStatus
    filled_quantity: float = 0.0
    avg_fill_price: float = 0.0


@dataclass
class PaperBroker:
    """Paper ExecutionAdapter。"""

    venue: Venue = Venue.BINANCE
    _orders: dict[str, _PaperOrder] = field(default_factory=dict)
    _books: dict[str, tuple[float, float]] = field(default_factory=dict)
    _outbox: list[ExecutionEvent] = field(default_factory=list)
    _fills: list[ExternalFill] = field(default_factory=list)
    _next_exchange_id: int = 0
    _reject_next: str | None = None
    _drop_next_ack: bool = False
    _defer_cancel_for: set[str] = field(default_factory=set)
    _execution_sequence: int = 0

    # ------------------------------------------------------------------ 盘口（venue rule 用）

    def update_book(self, symbol: str, *, best_bid: float, best_ask: float) -> None:
        if best_bid <= 0.0 or best_ask <= 0.0 or best_bid >= best_ask:
            raise ValueError("book must satisfy 0 < best_bid < best_ask")
        self._books[symbol] = (best_bid, best_ask)

    def clear_book(self, symbol: str) -> None:
        self._books.pop(symbol, None)

    # ------------------------------------------------------------------ 测试脚本

    def reject_next_submit(self, reason: str = PaperRejectReason.SCRIPTED_REJECT.value) -> None:
        self._reject_next = reason

    def drop_next_ack(self) -> None:
        """下一次 submit 不返回任何 ack（模拟「提交结果未知」→ 之后判 LOST）。"""
        self._drop_next_ack = True

    def defer_cancel_ack(self, client_order_id: str) -> None:
        """撤单不立即确认（cancel race 测试用，之后调用 `cancel_ack`）。"""
        self._defer_cancel_for.add(client_order_id)

    def drop_from_external(self, client_order_id: str) -> None:
        """从 broker 外部状态中移除订单（模拟「本地 OPEN、外部不存在」）。"""
        self._orders.pop(client_order_id, None)

    # ------------------------------------------------------------------ ExecutionAdapter

    def submit(self, order: Order) -> tuple[ExecutionEvent, ...]:
        if self._reject_next is not None:
            reason, self._reject_next = self._reject_next, None
            return (OrderRejected(order.client_order_id, reason, order.created_at),)
        if order.client_order_id in self._orders:
            return (
                OrderRejected(
                    order.client_order_id, PaperRejectReason.DUPLICATE_CLIENT_ORDER_ID.value, order.created_at
                ),
            )
        crossing = self._crosses_book(order)
        if order.post_only and crossing:
            return (
                OrderRejected(order.client_order_id, PaperRejectReason.REJECT_POST_ONLY.value, order.created_at),
            )

        self._next_exchange_id += 1
        exchange_order_id = f"paper-{self._next_exchange_id:06d}"
        self._orders[order.client_order_id] = _PaperOrder(
            client_order_id=order.client_order_id,
            exchange_order_id=exchange_order_id,
            symbol=order.symbol,
            side=order.side,
            price=order.price,
            quantity=order.quantity,
            status=OrderStatus.OPEN,
        )
        if self._drop_next_ack:
            # 订单在交易所侧确实存在，只是 ack 丢失（结果未知 → 之后走 LOST / reconciliation）
            self._drop_next_ack = False
            return ()
        return (OrderAccepted(order.client_order_id, exchange_order_id, order.created_at),)

    def cancel(self, order: Order) -> tuple[ExecutionEvent, ...]:
        known = self._orders.get(order.client_order_id)
        if known is None:
            return (
                OrderRejected(
                    order.client_order_id, PaperRejectReason.CANCEL_UNKNOWN_ORDER.value, order.updated_at
                ),
            )
        if order.client_order_id in self._defer_cancel_for:
            self._defer_cancel_for.discard(order.client_order_id)
            return ()
        # 同步确认：只返回事件，不再重复入 outbox（否则下一次 poll 会二次投递）
        return (self._apply_cancel_ack(order.client_order_id, timestamp=order.updated_at, enqueue=False),)

    def poll(self) -> tuple[ExecutionEvent, ...]:
        events = tuple(self._outbox)
        self._outbox.clear()
        return events

    def open_orders(self) -> tuple[ExternalOrder, ...]:
        return tuple(
            ExternalOrder(
                client_order_id=paper.client_order_id,
                exchange_order_id=paper.exchange_order_id,
                symbol=paper.symbol,
                status=paper.status,
                filled_quantity=paper.filled_quantity,
                avg_fill_price=paper.avg_fill_price,
                side=paper.side,  # type: ignore[arg-type]
                quantity=paper.quantity,
                price=paper.price,
            )
            for paper in sorted(self._orders.values(), key=lambda item: item.client_order_id)
            if not paper.status.is_terminal
        )

    def recent_fills(self, *, since_ms: Milliseconds | None = None) -> tuple[ExternalFill, ...]:
        return tuple(
            external for external in self._fills if since_ms is None or external.timestamp >= since_ms
        )

    # ------------------------------------------------------------------ 测试驱动 API

    def fill(
        self,
        client_order_id: str,
        *,
        quantity: float,
        price: float,
        fee: float = 0.0,
        fee_asset: str = "USDT",
        timestamp: Milliseconds,
        execution_id: str | None = None,
        trade_id: str | None = None,
        update_external: bool = True,
    ) -> str:
        """把一笔成交放进 outbox。返回 `execution_id`（便于构造重复事件）。"""
        self._execution_sequence += 1
        execution = execution_id or f"exec-{self._execution_sequence:06d}"
        trade = trade_id or f"trade-{self._execution_sequence:06d}"

        if update_external:
            self._apply_external_fill(
                client_order_id, quantity=quantity, price=price, execution=execution, trade=trade,
                timestamp=timestamp, fee=fee, fee_asset=fee_asset,
            )

        self._outbox.append(
            FillReceived(
                client_order_id=client_order_id,
                execution_id=execution,
                trade_id=trade,
                price=price,
                quantity=quantity,
                timestamp=timestamp,
                fee=fee,
                fee_asset=fee_asset,
            )
        )
        return execution

    def _apply_external_fill(
        self,
        client_order_id: str,
        *,
        quantity: float,
        price: float,
        execution: str,
        trade: str,
        timestamp: Milliseconds,
        fee: float,
        fee_asset: str,
    ) -> None:
        """更新 broker 侧订单状态与成交历史（终态不被 late fill 复活）。"""
        paper = self._orders.get(client_order_id)
        if paper is not None:
            new_filled = min(paper.quantity, paper.filled_quantity + quantity)
            paper.avg_fill_price = (
                (paper.avg_fill_price * paper.filled_quantity) + (price * quantity)
            ) / new_filled
            paper.filled_quantity = new_filled
            if not paper.status.is_terminal:
                paper.status = (
                    OrderStatus.FILLED if new_filled >= paper.quantity - 1e-9 else OrderStatus.PARTIALLY_FILLED
                )
        self._fills.append(
            ExternalFill(
                client_order_id=client_order_id,
                execution_id=execution,
                trade_id=trade,
                price=price,
                quantity=quantity,
                timestamp=timestamp,
                fee=fee,
                fee_asset=fee_asset,
            )
        )

    def duplicate_fill(
        self,
        client_order_id: str,
        *,
        execution_id: str,
        trade_id: str,
        quantity: float,
        price: float,
        timestamp: Milliseconds,
        fee: float = 0.0,
    ) -> None:
        """重复投递同一笔成交（execution 层去重测试用）。"""
        self.fill(
            client_order_id,
            quantity=quantity,
            price=price,
            fee=fee,
            timestamp=timestamp,
            execution_id=execution_id,
            trade_id=trade_id,
            update_external=False,
        )

    def cancel_ack(
        self,
        client_order_id: str,
        *,
        timestamp: Milliseconds,
        executed_quantity: float | None = None,
    ) -> tuple[ExecutionEvent, ...]:
        """撤单确认（可滞后于 fill 投递，用于 cancel race）：返回事件并放入 outbox。"""
        event = self._apply_cancel_ack(client_order_id, timestamp=timestamp, executed_quantity=executed_quantity, enqueue=True)
        return (event,)

    def _apply_cancel_ack(
        self,
        client_order_id: str,
        *,
        timestamp: Milliseconds,
        executed_quantity: float | None = None,
        enqueue: bool,
    ) -> ExecutionEvent:
        paper = self._orders.get(client_order_id)
        if paper is not None:
            paper.status = OrderStatus.CANCELED
            if executed_quantity is not None:
                paper.filled_quantity = min(float(executed_quantity), paper.quantity)
        event = OrderCanceled(client_order_id, timestamp, executed_quantity=executed_quantity)
        if enqueue:
            self._outbox.append(event)
        return event

    def status_update(
        self,
        client_order_id: str,
        *,
        status: OrderStatus,
        timestamp: Milliseconds,
        detail: str = "",
        filled_quantity: float | None = None,
    ) -> None:
        self._outbox.append(
            OrderStatusUpdate(
                client_order_id=client_order_id,
                status=status,
                timestamp=timestamp,
                detail=detail,
                filled_quantity=filled_quantity,
            )
        )

    def pending_events(self) -> int:
        return len(self._outbox)

    # ------------------------------------------------------------------ 内部

    def _crosses_book(self, order: Order) -> bool:
        book = self._books.get(order.symbol)
        if book is None:
            # 盘口未知 → 无法进行 post-only 校验（文档化：跳过，不做假设）
            return False
        best_bid, best_ask = book
        if order.side.value == "buy":
            return order.price >= best_ask
        return order.price <= best_bid


__all__ = ["PaperBroker", "PaperRejectReason"]
