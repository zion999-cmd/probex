"""OrderTracker：生命周期、去重、cancel race、late fill、视图与 pending exposure。"""

from __future__ import annotations

import unittest

from execution.events import (
    FillReceived,
    OrderAccepted,
    OrderCanceled,
    OrderExpired,
    OrderRejected,
    OrderStatusUpdate,
)
from execution.tracker import ExecutionEventOutcome, OrderTracker
from execution.types import IllegalOrderTransition, OrderNotFoundError, OrderStatus
from portfolio.types import Side
from risk.types import OrderProposal
from tests.support import BASE_TS


def _proposal(quantity: float = 1.0, price: float = 100.0, *, reduce_only: bool = False) -> OrderProposal:
    return OrderProposal(
        symbol="BTCUSDT", side=Side.BUY, quantity=quantity, price=price, reduce_only=reduce_only
    )


class TrackerLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tracker = OrderTracker(session_id="s1")
        self.order = self.tracker.create(_proposal(), timestamp=BASE_TS)

    def test_client_order_id_is_unique_sortable_and_restart_safe(self) -> None:
        self.assertEqual(self.order.client_order_id, "probex-s1-000001")
        second = self.tracker.create(_proposal(), timestamp=BASE_TS)
        self.assertEqual(second.client_order_id, "probex-s1-000002")
        self.assertLess(self.order.client_order_id, second.client_order_id)
        self.assertIn("s1", self.order.client_order_id)

    def test_full_lifecycle_to_filled(self) -> None:
        self.assertIs(self.order.status, OrderStatus.PENDING_CREATE)

        opened = self.tracker.on_event(OrderAccepted(self.order.client_order_id, "ex-1", BASE_TS + 1))
        self.assertIs(opened.outcome, ExecutionEventOutcome.ORDER_OPENED)
        self.assertIs(opened.order.status, OrderStatus.OPEN)
        self.assertEqual(opened.order.exchange_order_id, "ex-1")

        partial = self.tracker.on_event(
            FillReceived(self.order.client_order_id, "e1", "t1", 100.0, 0.4, BASE_TS + 2)
        )
        self.assertIs(partial.outcome, ExecutionEventOutcome.FILL_APPLIED)
        self.assertIs(partial.order.status, OrderStatus.PARTIALLY_FILLED)
        self.assertIsNotNone(partial.fill)

        filled = self.tracker.on_event(
            FillReceived(self.order.client_order_id, "e2", "t2", 101.0, 0.6, BASE_TS + 3)
        )
        self.assertIs(filled.outcome, ExecutionEventOutcome.FILL_COMPLETED)
        self.assertIs(filled.order.status, OrderStatus.FILLED)
        self.assertAlmostEqual(filled.order.avg_fill_price, 100.6)
        self.assertTrue(filled.order.is_terminal)

    def test_rejection_and_expiry_paths(self) -> None:
        rejected = self.tracker.on_event(OrderRejected(self.order.client_order_id, "REJECT_POST_ONLY", BASE_TS + 1))
        self.assertIs(rejected.order.status, OrderStatus.FAILED)
        self.assertEqual(rejected.order.lost_reason, "REJECT_POST_ONLY")

        other = self.tracker.create(_proposal(), timestamp=BASE_TS)
        self.tracker.on_event(OrderAccepted(other.client_order_id, "ex-2", BASE_TS + 1))
        expired = self.tracker.on_event(OrderExpired(other.client_order_id, BASE_TS + 2))
        self.assertIs(expired.outcome, ExecutionEventOutcome.ORDER_EXPIRED)
        self.assertIs(expired.order.status, OrderStatus.EXPIRED)

    def test_cancel_request_is_not_cancel_success(self) -> None:
        self.tracker.on_event(OrderAccepted(self.order.client_order_id, "ex-1", BASE_TS + 1))

        pending = self.tracker.mark_cancel_pending(self.order.client_order_id, timestamp=BASE_TS + 2)
        self.assertIs(pending.status, OrderStatus.PENDING_CANCEL)
        self.assertTrue(pending.is_active)

        confirmed = self.tracker.on_event(OrderCanceled(self.order.client_order_id, BASE_TS + 3))
        self.assertIs(confirmed.outcome, ExecutionEventOutcome.ORDER_CANCELED)
        self.assertIs(confirmed.order.status, OrderStatus.CANCELED)

    def test_cancel_of_terminal_order_is_rejected(self) -> None:
        self.tracker.on_event(OrderAccepted(self.order.client_order_id, "ex-1", BASE_TS + 1))
        self.tracker.on_event(OrderCanceled(self.order.client_order_id, BASE_TS + 2))
        with self.assertRaises(IllegalOrderTransition):
            self.tracker.mark_cancel_pending(self.order.client_order_id, timestamp=BASE_TS + 3)

    def test_events_for_unknown_order(self) -> None:
        update = self.tracker.on_event(FillReceived("nope", "e1", "t1", 100.0, 1.0, BASE_TS))
        self.assertIs(update.outcome, ExecutionEventOutcome.UNKNOWN_ORDER)
        self.assertIsNone(update.fill)

    def test_require_order_raises(self) -> None:
        with self.assertRaises(OrderNotFoundError):
            self.tracker.require_order("missing")

    def test_accepted_after_fill_does_not_regress_status(self) -> None:
        self.tracker.on_event(OrderAccepted(self.order.client_order_id, "ex-1", BASE_TS + 1))
        self.tracker.on_event(FillReceived(self.order.client_order_id, "e1", "t1", 100.0, 1.0, BASE_TS + 2))

        late_accept = self.tracker.on_event(OrderAccepted(self.order.client_order_id, "ex-1", BASE_TS + 3))

        self.assertIs(late_accept.outcome, ExecutionEventOutcome.IGNORED)
        self.assertIs(late_accept.order.status, OrderStatus.FILLED)
        self.assertEqual(late_accept.order.exchange_order_id, "ex-1")


class TrackerViewsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tracker = OrderTracker(session_id="s1")

    def test_active_recent_terminal_and_lost_views(self) -> None:
        active = self.tracker.create(_proposal(), timestamp=BASE_TS)
        self.tracker.on_event(OrderAccepted(active.client_order_id, "ex-1", BASE_TS + 1))
        terminal = self.tracker.create(_proposal(), timestamp=BASE_TS)
        self.tracker.on_event(OrderRejected(terminal.client_order_id, "nope", BASE_TS + 1))
        lost = self.tracker.create(_proposal(), timestamp=BASE_TS)
        self.tracker.mark_lost(lost.client_order_id, timestamp=BASE_TS + 2, reason="no ack")

        self.assertEqual([order.client_order_id for order in self.tracker.active()], [active.client_order_id])
        self.assertEqual(
            [order.client_order_id for order in self.tracker.recent_terminal()], [terminal.client_order_id]
        )
        self.assertEqual([order.client_order_id for order in self.tracker.lost()], [lost.client_order_id])
        self.assertEqual(len(self.tracker.orders), 3)

    def test_recent_terminal_respects_since_filter(self) -> None:
        order = self.tracker.create(_proposal(), timestamp=BASE_TS)
        self.tracker.on_event(OrderRejected(order.client_order_id, "nope", BASE_TS + 1_000))

        self.assertEqual(len(self.tracker.recent_terminal(since_ms=BASE_TS)), 1)
        self.assertEqual(len(self.tracker.recent_terminal(since_ms=BASE_TS + 2_000)), 0)

    def test_pending_unacked_detects_stuck_orders(self) -> None:
        order = self.tracker.create(_proposal(), timestamp=BASE_TS)

        self.assertEqual(self.tracker.pending_unacked(now_ms=BASE_TS + 500, timeout_ms=1_000), ())
        stuck = self.tracker.pending_unacked(now_ms=BASE_TS + 1_500, timeout_ms=1_000)
        self.assertEqual([item.client_order_id for item in stuck], [order.client_order_id])

        with self.assertRaises(ValueError):
            self.tracker.pending_unacked(now_ms=BASE_TS, timeout_ms=0)

    def test_mark_lost_then_restore(self) -> None:
        order = self.tracker.create(_proposal(), timestamp=BASE_TS)
        self.tracker.on_event(OrderAccepted(order.client_order_id, "ex-1", BASE_TS + 1))

        lost = self.tracker.mark_lost(order.client_order_id, timestamp=BASE_TS + 2, reason="missing externally")
        self.assertIs(lost.status, OrderStatus.LOST)
        self.assertTrue(lost.is_lost)

        restored = self.tracker.on_event(
            OrderStatusUpdate(order.client_order_id, OrderStatus.OPEN, BASE_TS + 3, detail="found again")
        )
        self.assertIs(restored.outcome, ExecutionEventOutcome.STATUS_UPDATED)
        self.assertIs(restored.order.status, OrderStatus.OPEN)

    def test_mark_lost_rejects_terminal_orders(self) -> None:
        order = self.tracker.create(_proposal(), timestamp=BASE_TS)
        self.tracker.on_event(OrderRejected(order.client_order_id, "nope", BASE_TS + 1))
        with self.assertRaises(IllegalOrderTransition):
            self.tracker.mark_lost(order.client_order_id, timestamp=BASE_TS + 2, reason="x")

    def test_pending_exposure_covers_partial_orders(self) -> None:
        order = self.tracker.create(_proposal(quantity=2.0, price=100.0), timestamp=BASE_TS)
        self.tracker.on_event(OrderAccepted(order.client_order_id, "ex-1", BASE_TS + 1))
        self.assertAlmostEqual(self.tracker.open_order_exposure(), 200.0)

        self.tracker.on_event(FillReceived(order.client_order_id, "e1", "t1", 100.0, 0.5, BASE_TS + 2))
        self.assertAlmostEqual(self.tracker.open_order_exposure(), 150.0)

        self.tracker.on_event(FillReceived(order.client_order_id, "e2", "t2", 100.0, 1.5, BASE_TS + 3))
        self.assertEqual(self.tracker.open_order_exposure(), 0.0)


class TrackerDuplicateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tracker = OrderTracker(session_id="s1")
        self.order = self.tracker.create(_proposal(), timestamp=BASE_TS)
        self.tracker.on_event(OrderAccepted(self.order.client_order_id, "ex-1", BASE_TS + 1))

    def test_duplicate_execution_id_is_ignored(self) -> None:
        first = self.tracker.on_event(FillReceived(self.order.client_order_id, "e1", "t1", 100.0, 0.3, BASE_TS + 2))
        duplicate = self.tracker.on_event(
            FillReceived(self.order.client_order_id, "e1", "t-other", 100.0, 0.3, BASE_TS + 3)
        )

        self.assertIs(first.outcome, ExecutionEventOutcome.FILL_APPLIED)
        self.assertIs(duplicate.outcome, ExecutionEventOutcome.FILL_DUPLICATE)
        self.assertIsNone(duplicate.fill)
        self.assertAlmostEqual(duplicate.order.filled_quantity, 0.3)
        self.assertEqual(self.tracker.duplicate_fill_count, 1)

    def test_duplicate_trade_id_is_ignored(self) -> None:
        self.tracker.on_event(FillReceived(self.order.client_order_id, "e1", "t1", 100.0, 0.3, BASE_TS + 2))
        duplicate = self.tracker.on_event(
            FillReceived(self.order.client_order_id, "e-other", "t1", 100.0, 0.3, BASE_TS + 3)
        )

        self.assertIs(duplicate.outcome, ExecutionEventOutcome.FILL_DUPLICATE)
        self.assertAlmostEqual(duplicate.order.filled_quantity, 0.3)

    def test_overfill_is_rejected(self) -> None:
        update = self.tracker.on_event(
            FillReceived(self.order.client_order_id, "e1", "t1", 100.0, 1.5, BASE_TS + 2)
        )

        self.assertIs(update.outcome, ExecutionEventOutcome.FILL_REJECTED)
        self.assertIsNone(update.fill)
        self.assertIn("overfill", update.detail)
        self.assertEqual(self.tracker.rejected_fill_count, 1)

    def test_duplicate_does_not_change_counts_of_applied_fills(self) -> None:
        self.tracker.on_event(FillReceived(self.order.client_order_id, "e1", "t1", 100.0, 0.2, BASE_TS + 2))
        self.tracker.on_event(FillReceived(self.order.client_order_id, "e1", "t1", 100.0, 0.2, BASE_TS + 3))

        self.assertEqual(self.tracker.applied_fill_count, 1)
        self.assertEqual(self.tracker.duplicate_fill_count, 1)


if __name__ == "__main__":
    unittest.main()
