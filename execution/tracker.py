"""OrderTracker：本地订单状态的权威（P0001.6 §2 / §4-§10）。

职责：

- 拥有 `Order` 状态机与 execution 层成交去重；
- 把外部 `ExecutionEvent` 收敛为 canonical `Fill`，交给 Accounting（本模块不直接改 Accounting）；
- 提供 active / recent_terminal / lost 三个逻辑视图与 pending exposure。

关键纪律：

- **cancel request ≠ cancel success**：只有 `OrderCanceled` 才进 CANCELED；
- PENDING_CANCEL 期间 Fill 合法（可 → PARTIALLY_FILLED / FILLED）；
- 终态之后到达、且成交时间 ≤ 终态时间的 **late fill 必须记账**，但不改变终态（终态与成交事实是两个维度）；
- 超出合法性窗口的 fill 一律拒绝（fail closed）。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum

from execution.events import (
    ExecutionEvent,
    FillReceived,
    OrderAccepted,
    OrderCanceled,
    OrderExpired,
    OrderRejected,
    OrderStatusUpdate,
)
from execution.types import (
    IllegalOrderTransition,
    Order,
    OrderNotFoundError,
    OrderStatus,
    can_transition,
)
from execution.events import ExecutionEventType
from market.events.types import Milliseconds, Venue
from portfolio.types import Fill
from risk.types import OrderProposal

#: client_order_id 前缀（唯一 / 可排序 / restart-safe）。
CLIENT_ORDER_ID_PREFIX = "probex"


class ExecutionEventOutcome(Enum):
    """一次事件处理的结果分类（供 telemetry 与测试）。"""

    ORDER_OPENED = "order_opened"
    ORDER_REJECTED = "order_rejected"
    ORDER_CANCELED = "order_canceled"
    ORDER_EXPIRED = "order_expired"
    FILL_APPLIED = "fill_applied"
    FILL_COMPLETED = "fill_completed"
    LATE_FILL_APPLIED = "late_fill_applied"
    FILL_DUPLICATE = "fill_duplicate"
    FILL_REJECTED = "fill_rejected"
    STATUS_UPDATED = "status_updated"
    UNKNOWN_ORDER = "unknown_order"
    IGNORED = "ignored"


@dataclass(frozen=True, slots=True)
class TrackerUpdate:
    """一次事件处理的结果。`fill` 非空时表示需要交给 AccountingCore 记账。"""

    event_type: ExecutionEventType | str
    outcome: ExecutionEventOutcome
    order: Order | None = None
    fill: Fill | None = None
    detail: str = ""

    @property
    def needs_accounting(self) -> bool:
        return self.fill is not None


@dataclass(frozen=True, slots=True)
class UnresolvedOrder:
    """资料不足、无法量化暴露的订单（fail closed：假设它仍然可能存在）。"""

    client_order_id: str
    reason: str


@dataclass
class OrderTracker:
    """本地订单状态权威。"""

    venue: Venue = Venue.BINANCE
    session_id: str = "s1"
    _orders: dict[str, Order] = field(default_factory=dict)
    _sequence: int = 0
    _seen_execution_ids: set[tuple[str, str]] = field(default_factory=set)
    _seen_trade_ids: set[tuple[str, str]] = field(default_factory=set)
    applied_fill_count: int = 0
    duplicate_fill_count: int = 0
    late_fill_count: int = 0
    rejected_fill_count: int = 0
    _unresolved: dict[str, UnresolvedOrder] = field(default_factory=dict)

    # ------------------------------------------------------------------ 创建

    def next_client_order_id(self) -> str:
        """`probex-{session}-{sequence:06d}`。"""
        self._sequence += 1
        return f"{CLIENT_ORDER_ID_PREFIX}-{self.session_id}-{self._sequence:06d}"

    def create(
        self,
        proposal: OrderProposal,
        *,
        timestamp: Milliseconds,
        client_order_id: str | None = None,
    ) -> Order:
        """由 `OrderProposal` 创建本地订单（PENDING_CREATE）。不发送任何请求。"""
        identifier = client_order_id or self.next_client_order_id()
        if identifier in self._orders:
            raise IllegalOrderTransition(f"client_order_id {identifier!r} already exists")
        order = Order(
            client_order_id=identifier,
            venue=self.venue,
            symbol=proposal.symbol,
            side=proposal.side,
            price=proposal.price,
            quantity=proposal.quantity,
            status=OrderStatus.PENDING_CREATE,
            created_at=timestamp,
            updated_at=timestamp,
            reduce_only=proposal.reduce_only,
            post_only=proposal.post_only,
        )
        self._orders[identifier] = order
        return order

    def register(self, order: Order, *, replace_existing: bool = False) -> Order:
        """注册一个已知订单（restart / recovery / externally adopted）。"""
        if order.client_order_id in self._orders and not replace_existing:
            raise IllegalOrderTransition(f"client_order_id {order.client_order_id!r} already registered")
        self._orders[order.client_order_id] = order
        return order

    # ------------------------------------------------------------------ 只读视图

    def order(self, client_order_id: str) -> Order | None:
        return self._orders.get(client_order_id)

    def require_order(self, client_order_id: str) -> Order:
        order = self._orders.get(client_order_id)
        if order is None:
            raise OrderNotFoundError(f"unknown client_order_id {client_order_id!r}")
        return order

    @property
    def orders(self) -> tuple[Order, ...]:
        """全部订单，按 client_order_id 排序（确定性）。"""
        return tuple(self._orders[key] for key in sorted(self._orders))

    def active(self) -> tuple[Order, ...]:
        """当前可能产生 exposure 的订单。"""
        return tuple(order for order in self.orders if order.is_active)

    def recent_terminal(self, *, since_ms: Milliseconds | None = None) -> tuple[Order, ...]:
        """已确认终态、等待 late fill 或审计的订单。"""
        return tuple(
            order
            for order in self.orders
            if order.is_terminal and (since_ms is None or order.updated_at >= since_ms)
        )

    def lost(self) -> tuple[Order, ...]:
        """需要 reconciliation 的订单。"""
        return tuple(order for order in self.orders if order.is_lost)

    def uncertain_orders(self) -> tuple[Order, ...]:
        """LOST 订单：状态不确定，但风险必须假设它仍然存在。"""
        return tuple(order for order in self.orders if order.is_lost)

    def confirmed_open_exposure(self) -> float:
        """ACTIVE 订单未成交部分的名义价值（按订单价格计的最坏情形）。"""
        return float(sum(order.notional for order in self.active()))

    def uncertain_exposure(self) -> float:
        """LOST 订单未成交部分的名义价值（`LOST` 不是终态，继续占用风险额度）。"""
        return float(sum(order.notional for order in self.uncertain_orders()))

    def total_pending_exposure(self) -> float:
        """`confirmed + uncertain` —— 这是 RiskSnapshot 应当使用的暴露。"""
        return self.confirmed_open_exposure() + self.uncertain_exposure()

    def open_order_exposure(self) -> float:
        """兼容别名：等于 `total_pending_exposure()`（P0001.6.1 起含 uncertain）。"""
        return self.total_pending_exposure()

    # ------------------------------------------------------------------ 资料不足的订单

    def note_unresolved_order(self, client_order_id: str, *, reason: str) -> UnresolvedOrder:
        """记录一个「已知存在但资料不足」的订单：暴露无法量化 → Risk 必须 fail closed。"""
        if not isinstance(client_order_id, str) or not client_order_id:
            raise ValueError("client_order_id must be a non-empty string")
        entry = UnresolvedOrder(client_order_id=client_order_id, reason=reason or "insufficient order data")
        self._unresolved[client_order_id] = entry
        return entry

    def clear_unresolved_order(self, client_order_id: str) -> bool:
        """资料补齐（或 operator 确认）之后释放该标记。返回是否确有该标记。"""
        return self._unresolved.pop(client_order_id, None) is not None

    def unresolved_orders(self) -> tuple[UnresolvedOrder, ...]:
        return tuple(self._unresolved[key] for key in sorted(self._unresolved))

    @property
    def has_unknown_exposure(self) -> bool:
        """是否存在无法量化的订单暴露（→ RiskGate 必须拒绝新增暴露）。"""
        return bool(self._unresolved)

    def pending_unacked(self, *, now_ms: Milliseconds, timeout_ms: Milliseconds) -> tuple[Order, ...]:
        """超时仍停留在 PENDING_CREATE 的订单（需要标记 LOST 或 reconcile）。"""
        if timeout_ms <= 0:
            raise ValueError("timeout_ms must be > 0")
        return tuple(
            order
            for order in self.active()
            if order.status is OrderStatus.PENDING_CREATE and now_ms - order.updated_at >= timeout_ms
        )

    # ------------------------------------------------------------------ 本地状态转换

    def mark_cancel_pending(self, client_order_id: str, *, timestamp: Milliseconds) -> Order:
        """记录「撤单已发送」。**这不代表撤单成功**。"""
        order = self.require_order(client_order_id)
        if order.status is OrderStatus.PENDING_CANCEL:
            return order
        if order.status not in {OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED}:
            raise IllegalOrderTransition(
                f"cannot request cancel for order in {order.status.value}"
            )
        return self._transition(order, OrderStatus.PENDING_CANCEL, timestamp=timestamp)

    def mark_lost(self, client_order_id: str, *, timestamp: Milliseconds, reason: str) -> Order:
        order = self.require_order(client_order_id)
        if order.status is OrderStatus.LOST:
            return order
        if order.status.is_terminal:
            raise IllegalOrderTransition(f"terminal order {client_order_id!r} cannot become LOST")
        return self._transition(order, OrderStatus.LOST, timestamp=timestamp, lost_reason=reason)

    # ------------------------------------------------------------------ 事件处理

    def on_event(self, event: ExecutionEvent) -> TrackerUpdate:
        if isinstance(event, FillReceived):
            return self._on_fill(event)
        if isinstance(event, OrderAccepted):
            return self._on_accepted(event)
        if isinstance(event, OrderCanceled):
            return self._on_canceled(event)
        if isinstance(event, OrderRejected):
            return self._on_rejected(event)
        if isinstance(event, OrderExpired):
            return self._on_expired(event)
        if isinstance(event, OrderStatusUpdate):
            return self._on_status_update(event)
        return TrackerUpdate(event_type=type(event).__name__, outcome=ExecutionEventOutcome.IGNORED)

    def _on_accepted(self, event: OrderAccepted) -> TrackerUpdate:
        order = self._orders.get(event.client_order_id)
        if order is None:
            return self._unknown(event.client_order_id)
        if order.status is OrderStatus.PENDING_CREATE:
            updated = self._transition(
                order, OrderStatus.OPEN, timestamp=event.timestamp, exchange_order_id=event.exchange_order_id
            )
            return TrackerUpdate(event.event_type, ExecutionEventOutcome.ORDER_OPENED, updated)
        # 订单可能已经因为更早到达的成交而推进：只补记 exchange_order_id，不回退状态
        updated = self._replace(
            order, exchange_order_id=order.exchange_order_id or event.exchange_order_id
        )
        return TrackerUpdate(event.event_type, ExecutionEventOutcome.IGNORED, updated, detail="accepted after progress")

    def _on_rejected(self, event: OrderRejected) -> TrackerUpdate:
        order = self._orders.get(event.client_order_id)
        if order is None:
            return self._unknown(event.client_order_id)
        if order.status is not OrderStatus.PENDING_CREATE:
            return TrackerUpdate(
                event.event_type, ExecutionEventOutcome.IGNORED, order, detail="rejected after progress"
            )
        updated = self._transition(
            order,
            OrderStatus.FAILED,
            timestamp=event.timestamp,
            exchange_order_id=event.exchange_order_id,
            lost_reason=event.reason,
        )
        return TrackerUpdate(event.event_type, ExecutionEventOutcome.ORDER_REJECTED, updated, detail=event.reason)

    def _on_canceled(self, event: OrderCanceled) -> TrackerUpdate:
        order = self._orders.get(event.client_order_id)
        if order is None:
            return self._unknown(event.client_order_id)
        if order.status.is_terminal:
            return TrackerUpdate(
                event.event_type, ExecutionEventOutcome.IGNORED, order, detail="cancel ack after terminal state"
            )
        changes: dict[str, object] = {}
        if event.executed_quantity is not None:
            executed = min(float(event.executed_quantity), order.quantity)
            if executed > order.filled_quantity:
                changes["filled_quantity"] = executed
        updated = self._transition(order, OrderStatus.CANCELED, timestamp=event.timestamp, **changes)
        return TrackerUpdate(event.event_type, ExecutionEventOutcome.ORDER_CANCELED, updated)

    def _on_expired(self, event: OrderExpired) -> TrackerUpdate:
        order = self._orders.get(event.client_order_id)
        if order is None:
            return self._unknown(event.client_order_id)
        if order.status.is_terminal:
            return TrackerUpdate(event.event_type, ExecutionEventOutcome.IGNORED, order)
        updated = self._transition(order, OrderStatus.EXPIRED, timestamp=event.timestamp)
        return TrackerUpdate(event.event_type, ExecutionEventOutcome.ORDER_EXPIRED, updated)

    def _on_status_update(self, event: OrderStatusUpdate) -> TrackerUpdate:
        order = self._orders.get(event.client_order_id)
        if order is None:
            return self._unknown(event.client_order_id)
        target = event.status
        if not isinstance(target, OrderStatus):
            raise IllegalOrderTransition(f"status update carries non-OrderStatus {target!r}")
        changes: dict[str, object] = {}
        if event.exchange_order_id:
            changes["exchange_order_id"] = event.exchange_order_id
        if event.filled_quantity is not None:
            changes["filled_quantity"] = min(float(event.filled_quantity), order.quantity)
        if event.avg_fill_price is not None and event.avg_fill_price > 0.0:
            changes["avg_fill_price"] = float(event.avg_fill_price)
        if target is OrderStatus.LOST:
            changes["lost_reason"] = event.detail or "status update"
        if target is order.status and not changes:
            return TrackerUpdate(event.event_type, ExecutionEventOutcome.IGNORED, order)
        if target is not order.status:
            updated = self._transition(order, target, timestamp=event.timestamp, **changes)
        else:
            updated = self._replace(order, updated_at=event.timestamp, **changes)
        return TrackerUpdate(event.event_type, ExecutionEventOutcome.STATUS_UPDATED, updated, detail=event.detail)

    def _on_fill(self, event: FillReceived) -> TrackerUpdate:
        order = self._orders.get(event.client_order_id)
        if order is None:
            return self._unknown(event.client_order_id)
        if self._is_duplicate(order, event):
            self.duplicate_fill_count += 1
            return TrackerUpdate(event.event_type, ExecutionEventOutcome.FILL_DUPLICATE, order)

        if order.filled_quantity + event.quantity > order.quantity + 1e-9:
            return self._reject_fill(
                order, event, detail=f"overfill: {order.filled_quantity} + {event.quantity} > {order.quantity}"
            )

        is_late = order.status.is_terminal
        if is_late and event.timestamp > order.updated_at:
            return self._reject_fill(
                order,
                event,
                detail=(
                    f"fill timestamp {event.timestamp} is after terminal timestamp {order.updated_at}; "
                    "outside the order's validity window"
                ),
            )

        self._mark_fill_seen(order, event)
        fill = self._canonical_fill(order, event)
        changes = self._fill_changes(order, event)

        if is_late:
            # 终态不变；只更新成交事实（final_executed_quantity）
            self.applied_fill_count += 1
            self.late_fill_count += 1
            updated = self._replace(
                order, updated_at=order.updated_at, final_executed_quantity=changes["filled_quantity"], **changes
            )
            return TrackerUpdate(event.event_type, ExecutionEventOutcome.LATE_FILL_APPLIED, updated, fill=fill)

        target = (
            OrderStatus.FILLED
            if changes["filled_quantity"] >= order.quantity - 1e-9
            else OrderStatus.PARTIALLY_FILLED
        )
        updated = self._transition(order, target, timestamp=event.timestamp, **changes)
        self.applied_fill_count += 1
        outcome = (
            ExecutionEventOutcome.FILL_COMPLETED
            if target is OrderStatus.FILLED
            else ExecutionEventOutcome.FILL_APPLIED
        )
        return TrackerUpdate(event.event_type, outcome, updated, fill=fill)

    @staticmethod
    def _fill_changes(order: Order, event: FillReceived) -> dict[str, float]:
        """成交量与加权均价的增量计算。"""
        new_filled = order.filled_quantity + event.quantity
        new_avg = (
            (order.avg_fill_price * order.filled_quantity) + (event.price * event.quantity)
        ) / new_filled
        return {"filled_quantity": new_filled, "avg_fill_price": new_avg}

    def _reject_fill(self, order: Order, event: FillReceived, *, detail: str) -> TrackerUpdate:
        self.rejected_fill_count += 1
        return TrackerUpdate(event.event_type, ExecutionEventOutcome.FILL_REJECTED, order, detail=detail)

    # ------------------------------------------------------------------ 内部

    def _is_duplicate(self, order: Order, event: FillReceived) -> bool:
        return (order.symbol, event.execution_id) in self._seen_execution_ids or (
            order.symbol,
            event.trade_id,
        ) in self._seen_trade_ids

    def _mark_fill_seen(self, order: Order, event: FillReceived) -> None:
        self._seen_execution_ids.add((order.symbol, event.execution_id))
        self._seen_trade_ids.add((order.symbol, event.trade_id))

    def _canonical_fill(self, order: Order, event: FillReceived) -> Fill:
        return Fill(
            fill_id=event.execution_id,
            order_id=order.client_order_id,
            venue=order.venue,
            symbol=order.symbol,
            side=order.side,
            price=event.price,
            quantity=event.quantity,
            fee=event.fee,
            fee_asset=event.fee_asset,
            trade_id=event.trade_id,
            exchange_ts=event.timestamp,
            receive_ts=event.timestamp,
        )

    def _transition(self, order: Order, target: OrderStatus, *, timestamp: Milliseconds, **changes: object) -> Order:
        if not can_transition(order.status, target):
            raise IllegalOrderTransition(
                f"illegal order transition {order.status.value} -> {target.value} for {order.client_order_id}"
            )
        updated = order.with_status(target, timestamp=timestamp, **changes)
        self._orders[order.client_order_id] = updated
        return updated

    def _replace(self, order: Order, *, updated_at: Milliseconds | None = None, **changes: object) -> Order:
        updates: dict[str, object] = dict(changes)
        if updated_at is not None:
            updates["updated_at"] = updated_at
        updated = replace(order, **updates)
        self._orders[order.client_order_id] = updated
        return updated

    @staticmethod
    def _unknown(client_order_id: str) -> TrackerUpdate:
        return TrackerUpdate(
            event_type="unknown",
            outcome=ExecutionEventOutcome.UNKNOWN_ORDER,
            detail=f"no local order for client_order_id {client_order_id!r}",
        )


__all__ = [
    "CLIENT_ORDER_ID_PREFIX",
    "ExecutionEventOutcome",
    "OrderTracker",
    "TrackerUpdate",
    "UnresolvedOrder",
]
