"""PaperBroker：accept / reject / partial / full / cancel / late / lost 与 post-only 规则（SC-8 / SC-9）。"""

from __future__ import annotations

import unittest

from execution.adapters.paper import PaperBroker, PaperRejectReason
from execution.events import ExecutionEventType
from execution.types import OrderStatus
from tests.execution_support import ExecutionStack
from tests.support import BASE_TS


class PaperBrokerSubmitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.broker = PaperBroker()

    def _order(self, **overrides: object):
        stack = ExecutionStack.build()
        proposal = stack.proposal(**overrides)  # type: ignore[arg-type]
        return stack.tracker.create(proposal, timestamp=BASE_TS)

    def test_submit_is_accepted_and_returns_exchange_id(self) -> None:
        events = self.broker.submit(self._order())

        self.assertEqual(len(events), 1)
        self.assertIs(events[0].event_type, ExecutionEventType.ORDER_ACCEPTED)
        self.assertTrue(events[0].exchange_order_id.startswith("paper-"))  # type: ignore[attr-defined]
        self.assertEqual(len(self.broker.open_orders()), 1)

    def test_duplicate_client_order_id_is_rejected(self) -> None:
        order = self._order()
        self.broker.submit(order)

        events = self.broker.submit(order)

        self.assertIs(events[0].event_type, ExecutionEventType.ORDER_REJECTED)
        self.assertEqual(events[0].reason, PaperRejectReason.DUPLICATE_CLIENT_ORDER_ID.value)  # type: ignore[attr-defined]

    def test_scripted_reject_and_dropped_ack(self) -> None:
        self.broker.reject_next_submit("EXCHANGE_SAYS_NO")
        events = self.broker.submit(self._order())
        self.assertEqual(events[0].reason, "EXCHANGE_SAYS_NO")  # type: ignore[attr-defined]

        self.broker.drop_next_ack()
        self.assertEqual(self.broker.submit(self._order()), ())

    def test_cancel_of_unknown_order_is_reported(self) -> None:
        order = self._order()
        events = self.broker.cancel(order)

        self.assertIs(events[0].event_type, ExecutionEventType.ORDER_REJECTED)
        self.assertEqual(events[0].reason, PaperRejectReason.CANCEL_UNKNOWN_ORDER.value)  # type: ignore[attr-defined]

    def test_cancel_is_acknowledged_by_default(self) -> None:
        order = self._order()
        self.broker.submit(order)

        events = self.broker.cancel(order)

        self.assertIs(events[0].event_type, ExecutionEventType.ORDER_CANCELED)
        self.assertEqual(self.broker.open_orders(), ())

    def test_deferred_cancel_ack(self) -> None:
        order = self._order()
        self.broker.submit(order)
        self.broker.defer_cancel_ack(order.client_order_id)

        self.assertEqual(self.broker.cancel(order), ())
        self.assertEqual(self.broker.pending_events(), 0)
        self.broker.cancel_ack(order.client_order_id, timestamp=BASE_TS + 5, executed_quantity=0.0)
        self.assertEqual([event.event_type for event in self.broker.poll()], [ExecutionEventType.ORDER_CANCELED])


class PaperBrokerPostOnlyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.broker = PaperBroker()
        self.stack = ExecutionStack.build()
        self.broker.update_book("BTCUSDT", best_bid=99.0, best_ask=101.0)

    def _order(self, *, side, price: float, post_only: bool = True):
        from portfolio.types import Side

        proposal = self.stack.proposal(side=side, price=price, post_only=post_only)
        return self.stack.tracker.create(proposal, timestamp=BASE_TS)

    def test_post_only_buy_crossing_the_ask_is_rejected(self) -> None:
        from portfolio.types import Side

        events = self.broker.submit(self._order(side=Side.BUY, price=101.0))

        self.assertIs(events[0].event_type, ExecutionEventType.ORDER_REJECTED)
        self.assertEqual(events[0].reason, PaperRejectReason.REJECT_POST_ONLY.value)  # type: ignore[attr-defined]

    def test_post_only_sell_crossing_the_bid_is_rejected(self) -> None:
        from portfolio.types import Side

        events = self.broker.submit(self._order(side=Side.SELL, price=99.0))

        self.assertEqual(events[0].reason, PaperRejectReason.REJECT_POST_ONLY.value)  # type: ignore[attr-defined]

    def test_post_only_inside_the_book_is_accepted(self) -> None:
        from portfolio.types import Side

        self.assertIs(self.broker.submit(self._order(side=Side.BUY, price=100.5))[0].event_type, ExecutionEventType.ORDER_ACCEPTED)
        self.assertIs(self.broker.submit(self._order(side=Side.SELL, price=99.5))[0].event_type, ExecutionEventType.ORDER_ACCEPTED)

    def test_non_post_only_may_cross(self) -> None:
        from portfolio.types import Side

        events = self.broker.submit(self._order(side=Side.BUY, price=101.0, post_only=False))
        self.assertIs(events[0].event_type, ExecutionEventType.ORDER_ACCEPTED)

    def test_unknown_book_skips_the_check(self) -> None:
        from portfolio.types import Side

        self.broker.clear_book("BTCUSDT")
        events = self.broker.submit(self._order(side=Side.BUY, price=101.0))

        self.assertIs(events[0].event_type, ExecutionEventType.ORDER_ACCEPTED)


class PaperBrokerDrivingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.broker = PaperBroker()
        self.stack = ExecutionStack.build()
        self.order = self.stack.tracker.create(self.stack.proposal(quantity=1.0), timestamp=BASE_TS)
        self.broker.submit(self.order)

    def test_partial_and_full_fills_update_external_state(self) -> None:
        self.broker.fill(self.order.client_order_id, quantity=0.2, price=100.0, timestamp=BASE_TS + 1)
        first = self.broker.poll()
        self.assertIs(first[0].event_type, ExecutionEventType.FILL_RECEIVED)
        external = self.broker.open_orders()[0]
        self.assertIs(external.status, OrderStatus.PARTIALLY_FILLED)
        self.assertAlmostEqual(external.filled_quantity, 0.2)

        self.broker.fill(self.order.client_order_id, quantity=0.8, price=101.0, timestamp=BASE_TS + 2)
        self.broker.poll()
        self.assertEqual(self.broker.open_orders(), ())  # FILLED → 不再挂单
        self.assertEqual(len(self.broker.recent_fills()), 2)

    def test_duplicate_fill_can_be_replayed_without_changing_external_state(self) -> None:
        execution_id = self.broker.fill(
            self.order.client_order_id, quantity=0.3, price=100.0, timestamp=BASE_TS + 1
        )
        self.broker.poll()

        self.broker.duplicate_fill(
            self.order.client_order_id,
            execution_id=execution_id,
            trade_id="t-dup",
            quantity=0.3,
            price=100.0,
            timestamp=BASE_TS + 2,
        )

        external = self.broker.open_orders()[0]
        self.assertAlmostEqual(external.filled_quantity, 0.3)
        self.assertEqual(len(self.broker.recent_fills()), 1)

    def test_status_update_and_drop_from_external(self) -> None:
        self.broker.status_update(
            self.order.client_order_id, status=OrderStatus.LOST, timestamp=BASE_TS + 1, detail="stream gap"
        )
        events = self.broker.poll()
        self.assertIs(events[0].event_type, ExecutionEventType.ORDER_STATUS_UPDATE)

        self.broker.drop_from_external(self.order.client_order_id)
        self.assertEqual(self.broker.open_orders(), ())

    def test_recent_fills_can_be_filtered(self) -> None:
        self.broker.fill(self.order.client_order_id, quantity=0.1, price=100.0, timestamp=BASE_TS + 1)
        self.broker.fill(self.order.client_order_id, quantity=0.1, price=100.0, timestamp=BASE_TS + 100)
        self.broker.poll()

        self.assertEqual(len(self.broker.recent_fills()), 2)
        self.assertEqual(len(self.broker.recent_fills(since_ms=BASE_TS + 50)), 1)

    def test_book_validation(self) -> None:
        with self.assertRaises(ValueError):
            self.broker.update_book("BTCUSDT", best_bid=101.0, best_ask=99.0)


if __name__ == "__main__":
    unittest.main()
