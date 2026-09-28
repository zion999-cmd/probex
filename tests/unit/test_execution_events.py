"""ExecutionEvent 类型与 ExecutionAdapter 契约。"""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from execution.adapters.base import ExecutionAdapter
from execution.events import (
    EVENT_TYPES,
    ExecutionEventType,
    FillReceived,
    OrderAccepted,
    OrderCanceled,
    OrderExpired,
    OrderRejected,
    OrderStatusUpdate,
)
from execution.types import OrderStatus
from market.events.types import Venue
from portfolio.types import Side
from risk.types import OrderProposal
from tests.execution_support import ExecutionStack
from tests.support import BASE_TS


class ExecutionEventTypeTest(unittest.TestCase):
    def test_event_type_vocabulary_is_fixed(self) -> None:
        self.assertEqual(
            [event_type.value for event_type in ExecutionEventType],
            [
                "order_accepted",
                "order_rejected",
                "order_canceled",
                "order_expired",
                "fill_received",
                "order_status_update",
            ],
        )

    def test_event_classes_are_frozen_and_tagged(self) -> None:
        events = (
            OrderAccepted("c1", "ex-1", BASE_TS),
            OrderRejected("c1", "nope", BASE_TS),
            OrderCanceled("c1", BASE_TS),
            OrderExpired("c1", BASE_TS),
            FillReceived("c1", "e1", "t1", 100.0, 1.0, BASE_TS),
            OrderStatusUpdate("c1", OrderStatus.LOST, BASE_TS, "reconcile"),
        )
        expected = (
            ExecutionEventType.ORDER_ACCEPTED,
            ExecutionEventType.ORDER_REJECTED,
            ExecutionEventType.ORDER_CANCELED,
            ExecutionEventType.ORDER_EXPIRED,
            ExecutionEventType.FILL_RECEIVED,
            ExecutionEventType.ORDER_STATUS_UPDATE,
        )
        for event, event_type in zip(events, expected):
            with self.subTest(event=type(event).__name__):
                self.assertIs(event.event_type, event_type)
        with self.assertRaises(FrozenInstanceError):
            events[0].client_order_id = "x"  # type: ignore[misc]

    def test_event_type_registry(self) -> None:
        self.assertEqual(len(EVENT_TYPES), 6)
        self.assertEqual(set(EVENT_TYPES), {type(event) for event in (
            OrderAccepted("c", "e", 1), OrderRejected("c", "r", 1), OrderCanceled("c", 1),
            OrderExpired("c", 1), FillReceived("c", "e", "t", 1.0, 1.0, 1),
            OrderStatusUpdate("c", OrderStatus.OPEN, 1),
        )})

    def test_fill_event_defaults(self) -> None:
        fill = FillReceived("c1", "e1", "t1", 100.0, 1.0, BASE_TS)
        self.assertEqual(fill.fee, 0.0)
        self.assertEqual(fill.fee_asset, "USDT")
        self.assertIsNone(fill.is_maker)


class ExecutionAdapterContractTest(unittest.TestCase):
    def test_paper_broker_satisfies_the_protocol(self) -> None:
        stack = ExecutionStack.build()
        self.assertIsInstance(stack.broker, ExecutionAdapter)

    def test_unknown_event_is_ignored_by_tracker(self) -> None:
        stack = ExecutionStack.build()
        update = stack.tracker.on_event("not an event")  # type: ignore[arg-type]
        self.assertIs(update.outcome.value, "ignored")

    def test_status_update_with_bad_status_is_rejected(self) -> None:
        from execution.types import IllegalOrderTransition

        stack = ExecutionStack.build()
        order = stack.submit_order(now_ms=BASE_TS)
        with self.assertRaises(IllegalOrderTransition):
            stack.tracker.on_event(OrderStatusUpdate(order.client_order_id, "OPEN", BASE_TS + 1))  # type: ignore[arg-type]

    def test_order_proposal_has_no_lifecycle_fields(self) -> None:
        proposal = OrderProposal(symbol="BTCUSDT", side=Side.BUY, quantity=1.0, price=100.0)
        for forbidden in ("client_order_id", "exchange_order_id", "status", "filled_quantity", "created_at"):
            with self.subTest(field=forbidden):
                self.assertFalse(hasattr(proposal, forbidden))

    def test_venue_is_owned_by_tracker(self) -> None:
        stack = ExecutionStack.build()
        self.assertIs(stack.order(stack.submit_order(now_ms=BASE_TS).client_order_id).venue, Venue.BINANCE)


if __name__ == "__main__":
    unittest.main()
