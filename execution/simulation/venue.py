"""SimulatedVenue：基于真实 Replay 市场事件的 event-level 成交模拟器（P0001.8）。

回答的问题是：**给定当时的真实 L2 Book + aggressor trade 事件，这个 Maker 挂单是否有
合理证据认为成交了？成交多少？什么时候成交？**

硬约束：

- **deterministic**：同一事件流（同一喂入顺序）得到完全相同的成交结果；不使用 wall-clock；
- **no-future**：订单只受 `entry_ts` 之后的事件影响，任何成交都由逐事件因果链产生；
- **conservative**：`Book decrease != Fill evidence`；`UNKNOWN` 队列绝不产生推测性成交；
- **explicit uncertainty**：`QueueState` / `FillInferenceState` / `FillReason` 全部进入 telemetry。

事件流处理顺序（每个 `MarketEvent`）：

```text
advance_clock(process_ts)   → 订单生效（+submit latency）、到期撤单确认
process_event(event)       → book: MarketBook 更新 → 队列状态同步
                             trade: 去重 → 逐订单推断成交
```

`on_market_event()` **不**返回事件：所有 `ExecutionEvent` 都进入 outbox，由 `poll()` 单一路径取出
（避免 P0001.6 中「同步确认 + outbox 二次投递」那类重复投递）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from execution.events import (
    ExecutionEvent,
    FillReceived,
    OrderAccepted,
    OrderCanceled,
    OrderRejected,
)
from execution.simulation.fees import FeeSchedule
from execution.simulation.latency import LatencyModel
from execution.simulation.queue import UNKNOWN_QUEUE, QueueEstimate, clear_queue, consume_queue, queue_from_visible
from execution.simulation.types import (
    FillInferenceState,
    FillReason,
    Liquidity,
    QueueState,
    RestingOrderView,
    SimulatedFill,
    SimulatedRejectReason,
    SimulationError,
)
from execution.types import ExternalFill, ExternalOrder, Order, OrderStatus
from market.book.market_book import MarketBook
from market.book.order_book import BookSide
from market.events.payloads import AggressorSide, BookDeltaPayload, BookSnapshotPayload, TradePayload, PriceLevel
from market.events.types import MarketEvent, Milliseconds, Venue
from market.health.state import BookHealth
from portfolio.types import Side

#: Fee / venue 约束：模拟交易所编号前缀。
DEFAULT_EXCHANGE_ID_PREFIX = "sim"

#: 数量相等判定容差（与 `execution.types.Order` 保持一致）。
_QUANTITY_EPSILON = 1e-9

_OPPOSITE: dict[Side, AggressorSide] = {Side.BUY: AggressorSide.SELL, Side.SELL: AggressorSide.BUY}


@dataclass(slots=True)
class _RestingOrder:
    """模拟器内部的挂单状态（仅 SimulatedVenue 可修改）。"""

    client_order_id: str
    exchange_order_id: str
    symbol: str
    side: Side
    price: float
    quantity: float
    status: OrderStatus
    submitted_at: Milliseconds
    entry_ts: Milliseconds
    entered: bool = False
    filled_quantity: float = 0.0
    avg_fill_price: float = 0.0
    queue: QueueEstimate = UNKNOWN_QUEUE
    initial_queue_ahead: float | None = None
    fill_inference: FillInferenceState = FillInferenceState.ACTIVE
    queue_rebuild_count: int = 0
    cancel_requested_at: Milliseconds | None = None
    cancel_effective_ts: Milliseconds | None = None
    cancel_emitted: bool = False
    last_event_ordinal: int | None = None
    fills: list[SimulatedFill] = field(default_factory=list)

    @property
    def remaining_quantity(self) -> float:
        return max(0.0, self.quantity - self.filled_quantity)


class SimulatedVenue:
    """实现 `ExecutionAdapter` 契约的事件级成交模拟器。"""

    def __init__(
        self,
        *,
        symbol: str,
        latency: LatencyModel,
        fee_schedule: FeeSchedule,
        venue: Venue = Venue.BINANCE,
        book: MarketBook | None = None,
        exchange_id_prefix: str = DEFAULT_EXCHANGE_ID_PREFIX,
    ) -> None:
        if not isinstance(symbol, str) or not symbol:
            raise SimulationError("symbol must be a non-empty string")
        if not isinstance(latency, LatencyModel):
            raise SimulationError("latency must be a LatencyModel")
        if not isinstance(fee_schedule, FeeSchedule):
            raise SimulationError("fee_schedule must be a FeeSchedule")
        if not isinstance(venue, Venue):
            raise SimulationError("venue must be a Venue")
        if not isinstance(exchange_id_prefix, str) or not exchange_id_prefix:
            raise SimulationError("exchange_id_prefix must be a non-empty string")
        self._symbol = symbol
        self._venue = venue
        self._latency = latency
        self._fee_schedule = fee_schedule
        self._book = book if book is not None else MarketBook(venue, symbol)
        self._exchange_id_prefix = exchange_id_prefix
        self._orders: dict[str, _RestingOrder] = {}
        self._outbox: list[ExecutionEvent] = []
        self._fills: list[ExternalFill] = []
        self._seen_trade_ids: set[int] = set()
        self._duplicate_trades = 0
        self._event_ordinal = 0
        self._clock: Milliseconds | None = None
        self._next_exchange_id = 0

    # ------------------------------------------------------------------ 只读状态

    @property
    def symbol(self) -> str:
        return self._symbol

    @property
    def venue(self) -> Venue:
        return self._venue

    @property
    def book(self) -> MarketBook:
        return self._book

    @property
    def event_ordinal(self) -> int:
        """已处理的市场事件数（0 基序号；等于喂入顺序）。"""
        return self._event_ordinal

    @property
    def duplicate_trade_count(self) -> int:
        return self._duplicate_trades

    @property
    def fills(self) -> tuple[SimulatedFill, ...]:
        """全部模拟成交及证据（按产生顺序）。"""
        return tuple(self._fills_records())

    @property
    def pending_events(self) -> int:
        return len(self._outbox)

    def order_view(self, client_order_id: str) -> RestingOrderView:
        return _view(self._require(client_order_id))

    def order_views(self) -> tuple[RestingOrderView, ...]:
        return tuple(_view(order) for order in self._ordered())

    # ------------------------------------------------------------------ ExecutionAdapter

    def submit(self, order: Order) -> tuple[ExecutionEvent, ...]:
        """接受订单（venue rule：重复 id / post-only 穿价拒绝）；**不**立即成交。"""
        if order.client_order_id in self._orders:
            return (
                OrderRejected(
                    order.client_order_id,
                    SimulatedRejectReason.DUPLICATE_CLIENT_ORDER_ID.value,
                    order.created_at,
                ),
            )
        if order.post_only and self._crosses_book(order):
            return (
                OrderRejected(
                    order.client_order_id, SimulatedRejectReason.REJECT_POST_ONLY.value, order.created_at
                ),
            )
        self._next_exchange_id += 1
        exchange_order_id = f"{self._exchange_id_prefix}-{self._next_exchange_id:06d}"
        self._orders[order.client_order_id] = _RestingOrder(
            client_order_id=order.client_order_id,
            exchange_order_id=exchange_order_id,
            symbol=order.symbol,
            side=order.side,
            price=order.price,
            quantity=order.quantity,
            status=OrderStatus.OPEN,
            submitted_at=order.created_at,
            entry_ts=self._latency.entry_ts(order.created_at),
        )
        if self._clock is not None:
            # 若当前模拟时钟已达生效时刻，立刻建立队列估计（只用**已发生**的盘口，不看未来）
            self._advance_clock(self._clock)
        return (OrderAccepted(order.client_order_id, exchange_order_id, order.created_at),)

    def cancel(self, order: Order) -> tuple[ExecutionEvent, ...]:
        """撤单请求：延迟生效。返回确认事件（为空表示尚未生效）。"""
        entry = self._orders.get(order.client_order_id)
        if entry is None:
            return (
                OrderRejected(
                    order.client_order_id, SimulatedRejectReason.CANCEL_UNKNOWN_ORDER.value, order.updated_at
                ),
            )
        if entry.status.is_terminal or entry.cancel_requested_at is not None:
            return ()
        entry.cancel_requested_at = order.updated_at
        effective = self._latency.cancel_effective_ts(order.updated_at)
        entry.cancel_effective_ts = effective
        if self._clock is not None and self._clock >= effective:
            return (self._emit_cancel(entry, effective, enqueue=False),)
        return ()

    def poll(self) -> tuple[ExecutionEvent, ...]:
        """取出待投递的外部事件（含到期撤单确认）。"""
        if self._clock is not None:
            self._advance_clock(self._clock)
        events = tuple(self._outbox)
        self._outbox.clear()
        return events

    def open_orders(self) -> tuple[ExternalOrder, ...]:
        return tuple(
            ExternalOrder(
                client_order_id=order.client_order_id,
                exchange_order_id=order.exchange_order_id,
                symbol=order.symbol,
                status=order.status,
                filled_quantity=order.filled_quantity,
                avg_fill_price=order.avg_fill_price,
                side=order.side,
                quantity=order.quantity,
                price=order.price,
            )
            for order in self._ordered()
            if not order.status.is_terminal
        )

    def recent_fills(self, *, since_ms: Milliseconds | None = None) -> tuple[ExternalFill, ...]:
        return tuple(fill for fill in self._fills if since_ms is None or fill.timestamp >= since_ms)

    # ------------------------------------------------------------------ 市场事件入口

    def on_market_event(self, event: MarketEvent) -> None:
        """喂入一条市场事件；产生的事件进入 outbox，由 `poll()` 取出。"""
        self._require_event(event)
        ordinal = self._event_ordinal
        self._event_ordinal += 1
        self._advance_clock(event.process_ts)
        payload = event.payload
        if isinstance(payload, TradePayload):
            self._on_trade(event, payload, ordinal)
        elif isinstance(payload, (BookSnapshotPayload, BookDeltaPayload)):
            self._book.on_market_event(event)
            self._sync_orders_with_book(ordinal)
        else:  # pragma: no cover - MarketPayload 联合已封闭
            raise SimulationError(f"unsupported payload type: {type(payload).__name__}")

    # ------------------------------------------------------------------ 内部：时钟与生效

    def _advance_clock(self, now: Milliseconds) -> None:
        """把模拟时钟推进到 `now`：订单生效、到期撤单确认。

        模拟时钟**只**由市场事件（以及 `poll()` 的到期重估）推进；订单生效与撤单生效都以它为准，
        因此不会出现「用未来数据提前成交 / 提前撤单」。
        """
        self._clock = now if self._clock is None else max(self._clock, now)
        for order in self._ordered():
            if order.status.is_terminal:
                continue
            if not order.entered and self._clock >= order.entry_ts:
                order.entered = True
                self._establish_queue(order)
            effective = order.cancel_effective_ts
            if (
                order.cancel_requested_at is not None
                and not order.cancel_emitted
                and effective is not None
                and self._clock >= effective
            ):
                self._emit_cancel(order, effective, enqueue=True)

    def _establish_queue(self, order: _RestingOrder) -> None:
        """在**健康且可观察**的盘口上建立队列估计；否则保持 UNKNOWN。"""
        if order.queue.is_known or self._book.health is not BookHealth.HEALTHY:
            return
        queue = queue_from_visible(self._visible_size(order))
        if queue.is_unknown:
            return
        order.queue = queue
        if order.initial_queue_ahead is None:
            order.initial_queue_ahead = queue.ahead
        else:
            order.queue_rebuild_count += 1

    def _visible_size(self, order: _RestingOrder) -> float | None:
        """订单价位在当前 L2 中的可见数量；价位不可观察时为 None（未知 ≠ 0）。"""
        side = BookSide.BID if order.side is Side.BUY else BookSide.ASK
        return _level_size(self._book.depth(side), order.price)

    def _sync_orders_with_book(self, ordinal: int) -> None:
        """盘口变化后同步订单状态：不可信 → 挂起并作废队列；恢复 → 重建或保持 UNKNOWN。"""
        healthy = self._book.health is BookHealth.HEALTHY
        for order in self._ordered():
            if order.status.is_terminal:
                continue
            order.last_event_ordinal = ordinal
            if not healthy:
                # §7：不得继续推导成交；已有估计作废，且不得在恢复后无声续算
                order.fill_inference = FillInferenceState.SUSPENDED
                order.queue = UNKNOWN_QUEUE
                continue
            if order.fill_inference is FillInferenceState.SUSPENDED:
                order.fill_inference = FillInferenceState.ACTIVE
                order.queue = UNKNOWN_QUEUE
            if order.entered:
                self._establish_queue(order)

    # ------------------------------------------------------------------ 内部：成交推断

    def _on_trade(self, event: MarketEvent, payload: TradePayload, ordinal: int) -> None:
        if payload.aggregate_trade_id in self._seen_trade_ids:
            # SC-5：重复投递的同一笔成交不得重复推进队列或重复成交
            self._duplicate_trades += 1
            return
        self._seen_trade_ids.add(payload.aggregate_trade_id)
        for order in self._ordered():
            self._apply_trade(order, payload, event, ordinal)

    def _apply_trade(self, order: _RestingOrder, payload: TradePayload, event: MarketEvent, ordinal: int) -> None:
        order.last_event_ordinal = ordinal
        if order.status.is_terminal or not order.entered:
            return
        if order.fill_inference is FillInferenceState.SUSPENDED or order.queue.is_unknown:
            return
        if payload.aggressor is not _OPPOSITE[order.side]:  # 只有对手方向 aggressor 才消耗队列
            return
        reason = _fill_reason(order, payload.price)
        if reason is None:
            return
        if reason is FillReason.QUEUE_CONSUMED:
            queue, fillable = consume_queue(order.queue, payload.quantity)
        else:
            queue, fillable = clear_queue(order.queue), payload.quantity
        order.queue = queue
        quantity = min(fillable, order.remaining_quantity)
        if quantity <= 0.0:  # 只推进了队列
            return
        self._emit_fill(order, quantity=quantity, reason=reason, payload=payload, event=event, ordinal=ordinal)

    def _emit_fill(
        self,
        order: _RestingOrder,
        *,
        quantity: float,
        reason: FillReason,
        payload: TradePayload,
        event: MarketEvent,
        ordinal: int,
    ) -> None:
        """生成 maker 成交：价格用订单限价，手续费由显式 FeeSchedule 决定（§11 / §12）。"""
        record = self._fill_record(
            order, quantity=quantity, reason=reason, payload=payload, event=event, ordinal=ordinal
        )
        order.fills.append(record)
        self._apply_fill_to_order(order, quantity=quantity)
        self._fills.append(
            ExternalFill(
                client_order_id=order.client_order_id,
                execution_id=record.execution_id,
                trade_id=record.trade_id,
                price=record.price,
                quantity=record.quantity,
                timestamp=record.exchange_ts,
                fee=record.fee,
                fee_asset=record.fee_asset,
            )
        )
        self._outbox.append(
            FillReceived(
                client_order_id=order.client_order_id,
                execution_id=record.execution_id,
                trade_id=record.trade_id,
                price=record.price,
                quantity=record.quantity,
                timestamp=record.exchange_ts,
                fee=record.fee,
                fee_asset=record.fee_asset,
                is_maker=True,
            )
        )

    def _fill_record(
        self,
        order: _RestingOrder,
        *,
        quantity: float,
        reason: FillReason,
        payload: TradePayload,
        event: MarketEvent,
        ordinal: int,
    ) -> SimulatedFill:
        """成交证据（含 queue 状态、reason、事件序号），供研究时判断可信度。"""
        return SimulatedFill(
            client_order_id=order.client_order_id,
            exchange_order_id=order.exchange_order_id,
            symbol=order.symbol,
            side=order.side,
            price=order.price,
            quantity=quantity,
            fee=self._fee_schedule.maker_fee(price=order.price, quantity=quantity),
            fee_asset=self._fee_schedule.fee_asset,
            liquidity=Liquidity.MAKER,
            fill_reason=reason,
            queue_state=QueueState.KNOWN,
            event_ordinal=ordinal,
            exchange_ts=event.exchange_ts,
            execution_id=f"{self._exchange_id_prefix}-exec-{payload.aggregate_trade_id}-{order.client_order_id}",
            trade_id=f"{self._exchange_id_prefix}-trade-{payload.aggregate_trade_id}-{order.client_order_id}",
            aggregate_trade_id=payload.aggregate_trade_id,
        )

    def _apply_fill_to_order(self, order: _RestingOrder, *, quantity: float) -> None:
        """更新 simulated venue 侧的订单事实（累计量 / 均价 / 状态）。"""
        new_filled = min(order.quantity, order.filled_quantity + quantity)
        order.avg_fill_price = (
            (order.avg_fill_price * order.filled_quantity) + (order.price * quantity)
        ) / new_filled
        order.filled_quantity = new_filled
        order.status = (
            OrderStatus.FILLED
            if new_filled >= order.quantity - _QUANTITY_EPSILON
            else OrderStatus.PARTIALLY_FILLED
        )

    def _emit_cancel(self, order: _RestingOrder, timestamp: Milliseconds, *, enqueue: bool) -> OrderCanceled:
        order.cancel_emitted = True
        order.status = OrderStatus.CANCELED
        event = OrderCanceled(order.client_order_id, timestamp, executed_quantity=order.filled_quantity)
        if enqueue:
            self._outbox.append(event)
        return event

    # ------------------------------------------------------------------ 内部：杂项

    def _crosses_book(self, order: Order) -> bool:
        """post-only venue rule：盘口未知（未同步）时不做判断。"""
        if self._book.health is not BookHealth.HEALTHY:
            return False
        best_bid = self._book.best_bid()
        best_ask = self._book.best_ask()
        if best_bid is None or best_ask is None:
            return False
        if order.side is Side.BUY:
            return order.price >= best_ask.price
        return order.price <= best_bid.price

    def _fills_records(self) -> list[SimulatedFill]:
        return [fill for order in self._ordered() for fill in order.fills]

    def _require(self, client_order_id: str) -> _RestingOrder:
        try:
            return self._orders[client_order_id]
        except KeyError:
            raise SimulationError(f"unknown client_order_id {client_order_id!r}") from None

    def _ordered(self) -> list[_RestingOrder]:
        """确定性顺序：entry_ts → client_order_id。"""
        return sorted(self._orders.values(), key=lambda order: (order.entry_ts, order.client_order_id))

    def _require_event(self, event: MarketEvent) -> None:
        if not isinstance(event, MarketEvent):
            raise SimulationError(f"expected a MarketEvent, got {type(event).__name__}")
        if event.venue is not self._venue or event.symbol != self._symbol:
            raise SimulationError(
                f"SimulatedVenue({self._venue.value}/{self._symbol}) received event for "
                f"{event.venue.value}/{event.symbol}"
            )


def _view(order: _RestingOrder) -> RestingOrderView:
    return RestingOrderView(
        client_order_id=order.client_order_id,
        exchange_order_id=order.exchange_order_id,
        symbol=order.symbol,
        side=order.side,
        price=order.price,
        quantity=order.quantity,
        filled_quantity=order.filled_quantity,
        status=order.status,
        entered=order.entered,
        entry_exchange_ts=order.entry_ts,
        queue_state=order.queue.state,
        queue_ahead=order.queue.ahead,
        initial_queue_ahead=order.initial_queue_ahead,
        fill_inference=order.fill_inference,
        queue_rebuild_count=order.queue_rebuild_count,
        cancel_requested_at=order.cancel_requested_at,
        cancel_effective_ts=order.cancel_effective_ts,
        last_event_ordinal=order.last_event_ordinal,
        fills=tuple(order.fills),
    )


def _level_size(levels: tuple[PriceLevel, ...], price: float) -> float | None:
    """精确匹配档位价格（价格来自同一套 tick 语义）。"""
    for level in levels:
        if level.price == price:
            return level.size
    return None


def _fill_reason(order: _RestingOrder, trade_price: float) -> FillReason | None:
    """成交价与订单限价的关系 → 证据类型；未触及订单价位时返回 None。"""
    if trade_price == order.price:
        return FillReason.QUEUE_CONSUMED
    if order.side is Side.BUY:
        return FillReason.TRADE_THROUGH if trade_price < order.price else None
    return FillReason.TRADE_THROUGH if trade_price > order.price else None


__all__ = ["DEFAULT_EXCHANGE_ID_PREFIX", "SimulatedVenue"]
