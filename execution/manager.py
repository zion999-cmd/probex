"""OrderManager：把 `OrderProposal` 变成真实订单，并驱动 submit / cancel / replace。

职责边界：

- 拥有 `OrderTracker`（本地状态权威）与 `ExecutionAdapter`（外部事实）；
- **不**做风险判断（那是 `ExecutionEngine` + `RiskGate` 的职责）；
- **不**直接修改 Accounting（canonical Fill 由调用方交给 `AccountingCore`）；
- replace 严格遵守 cancel-before-replace：旧单必须进入**已确认终态**才允许下新单。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from execution.adapters.base import ExecutionAdapter
from execution.events import ExecutionEvent
from execution.tracker import OrderTracker, TrackerUpdate
from execution.types import Order, OrderNotFoundError, OrderStatus
from market.events.types import Milliseconds
from risk.types import OrderProposal


@dataclass(frozen=True, slots=True)
class ReplaceOutcome:
    """一次 `replace` 调用的结果。"""

    requested_cancel: bool = False
    cancel_updates: tuple[TrackerUpdate, ...] = ()
    order: Order | None = None
    updates: tuple[TrackerUpdate, ...] = ()
    detail: str = ""

    @property
    def replaced(self) -> bool:
        return self.order is not None


@dataclass
class OrderManager:
    """订单生命周期编排（submit / cancel / replace / poll）。"""

    tracker: OrderTracker
    adapter: ExecutionAdapter

    def submit(self, proposal: OrderProposal, *, timestamp: Milliseconds) -> tuple[Order, tuple[TrackerUpdate, ...]]:
        """创建本地订单并提交给 adapter（不做风险判断）。"""
        order = self.tracker.create(proposal, timestamp=timestamp)
        events = self.adapter.submit(order)
        updates = self.on_events(events)
        # 事件处理之后再取快照：返回的 Order 必须反映 ack 之后的状态
        return self.tracker.require_order(order.client_order_id), updates

    def cancel(self, client_order_id: str, *, timestamp: Milliseconds) -> tuple[TrackerUpdate, ...]:
        """请求撤单：先本地标记 PENDING_CANCEL，再发请求；**只有确认事件才进 CANCELED**。"""
        order = self.tracker.require_order(client_order_id)
        if order.is_terminal:
            raise OrderNotFoundError(f"order {client_order_id!r} is already terminal ({order.status.value})")
        if order.status is not OrderStatus.PENDING_CANCEL:
            self.tracker.mark_cancel_pending(client_order_id, timestamp=timestamp)
        pending = self.tracker.require_order(client_order_id)
        events = self.adapter.cancel(pending)
        return self.on_events(events)

    def replace(
        self,
        client_order_id: str,
        proposal: OrderProposal,
        *,
        timestamp: Milliseconds,
    ) -> ReplaceOutcome:
        """cancel-before-replace：旧单确认终态后才下新单。"""
        old = self.tracker.require_order(client_order_id)
        if not old.is_terminal:
            updates = self.cancel(client_order_id, timestamp=timestamp)
            return ReplaceOutcome(
                requested_cancel=True,
                cancel_updates=updates,
                detail="old order is not in a confirmed terminal state; cancel requested first",
            )

        new_order, updates = self.submit(proposal, timestamp=timestamp)
        return ReplaceOutcome(order=new_order, updates=updates, detail="old order terminal; new order submitted")

    def on_events(self, events: tuple[ExecutionEvent, ...]) -> tuple[TrackerUpdate, ...]:
        """把外部事件喂给 tracker（顺序敏感：按到达顺序处理）。"""
        return tuple(self.tracker.on_event(event) for event in events)

    def poll(self) -> tuple[TrackerUpdate, ...]:
        """拉取并处理 adapter 的外部事件。"""
        return self.on_events(self.adapter.poll())

    def open_order_exposure(self) -> float:
        """`confirmed + uncertain` 暴露（供下一次 RiskSnapshot）。"""
        return self.tracker.total_pending_exposure()

    @property
    def confirmed_open_exposure(self) -> float:
        return self.tracker.confirmed_open_exposure()

    @property
    def uncertain_exposure(self) -> float:
        return self.tracker.uncertain_exposure()

    @property
    def unresolved_order_count(self) -> int:
        return len(self.tracker.unresolved_orders())

    @property
    def active_orders(self) -> tuple[Order, ...]:
        return self.tracker.active()

    @property
    def orders(self) -> tuple[Order, ...]:
        return self.tracker.orders

    def orders_for_decision(self, decision_id: str) -> tuple[Order, ...]:
        """`Decision → Order(s)` 反查（由 tracker 的 canonical correlation 提供）。"""
        return self.tracker.orders_for_decision(decision_id)


__all__ = ["OrderManager", "ReplaceOutcome"]
