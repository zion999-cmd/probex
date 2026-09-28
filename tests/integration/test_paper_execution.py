"""Integration：Paper 执行链路的完整生命周期（SC-1 / SC-7）。"""

from __future__ import annotations

import unittest

from execution.types import OrderStatus
from portfolio.types import Side
from risk.limits import RiskLimits
from tests.execution_support import ExecutionStack
from tests.support import BASE_TS


class PaperLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExecutionStack.build(limits=RiskLimits(max_position_qty=10.0))

    def test_sc1_pending_create_open_partially_filled_filled(self) -> None:
        order = self.stack.submit_order(self.stack.proposal(quantity=1.0), now_ms=BASE_TS)
        self.assertIs(order.status, OrderStatus.OPEN)  # submit 后已收到 ack
        self.assertTrue(order.exchange_order_id.startswith("paper-"))

        first = self.stack.fill(order, quantity=0.2, timestamp=BASE_TS + 1)
        self.assertIs(self.stack.order(order.client_order_id).status, OrderStatus.PARTIALLY_FILLED)
        second = self.stack.fill(order, quantity=0.3, timestamp=BASE_TS + 2)
        self.assertIs(self.stack.order(order.client_order_id).status, OrderStatus.PARTIALLY_FILLED)
        third = self.stack.fill(order, quantity=0.5, timestamp=BASE_TS + 3)

        final = self.stack.order(order.client_order_id)
        self.assertIs(final.status, OrderStatus.FILLED)
        self.assertAlmostEqual(final.filled_quantity, 1.0)
        self.assertEqual([len(result.fills) for result in (first, second, third)], [1, 1, 1])
        self.assertEqual(final.final_executed_quantity, 1.0)

    def test_pending_create_is_visible_before_ack(self) -> None:
        self.stack.broker.drop_next_ack()
        result = self.stack.submit(self.stack.proposal(), now_ms=BASE_TS)

        order = self.stack.order(result.order.client_order_id)
        self.assertIs(order.status, OrderStatus.PENDING_CREATE)
        stuck = self.stack.tracker.pending_unacked(now_ms=BASE_TS + 2_000, timeout_ms=1_000)
        self.assertEqual([item.client_order_id for item in stuck], [order.client_order_id])

    def test_rejection_keeps_accounting_untouched(self) -> None:
        self.stack.broker.reject_next_submit("REJECT_POST_ONLY")
        result = self.stack.submit(self.stack.proposal(), now_ms=BASE_TS)

        self.assertIs(self.stack.order(result.order.client_order_id).status, OrderStatus.FAILED)
        self.assertEqual(self.stack.accounting.fills.count, 0)
        self.assertEqual(self.stack.accounting.position(self.stack.symbol).qty, 0.0)

    def test_cancel_then_late_fill_keeps_terminal_status(self) -> None:
        order = self.stack.submit_order(self.stack.proposal(quantity=1.0), now_ms=BASE_TS)
        self.stack.fill(order, quantity=0.4, timestamp=BASE_TS + 1)
        self.stack.cancel(order, now_ms=BASE_TS + 2)

        self.assertIs(self.stack.order(order.client_order_id).status, OrderStatus.CANCELED)

        late = self.stack.fill(order, quantity=0.3, timestamp=BASE_TS + 2)
        self.assertEqual([update.outcome.value for update in late.updates], ["late_fill_applied"])

        final = self.stack.order(order.client_order_id)
        self.assertIs(final.status, OrderStatus.CANCELED)
        self.assertAlmostEqual(final.executed_quantity, 0.7)
        self.assertAlmostEqual(self.stack.accounting.position(self.stack.symbol).qty, 0.7)

    def test_sc7_replace_waits_for_confirmed_terminal_state(self) -> None:
        old = self.stack.submit_order(self.stack.proposal(quantity=1.0, price=100.0), now_ms=BASE_TS)
        self.stack.broker.defer_cancel_ack(old.client_order_id)

        first_attempt = self.stack.manager.replace(
            old.client_order_id, self.stack.proposal(quantity=1.0, price=101.0), timestamp=BASE_TS + 1
        )
        self.assertTrue(first_attempt.requested_cancel)
        self.assertFalse(first_attempt.replaced)
        self.assertEqual(len(self.stack.tracker.active()), 1)  # 只有旧单（PENDING_CANCEL）
        self.assertIs(self.stack.order(old.client_order_id).status, OrderStatus.PENDING_CANCEL)

        # 撤单确认之后才允许下新单
        self.stack.broker.cancel_ack(old.client_order_id, timestamp=BASE_TS + 2)
        self.stack.poll(now_ms=BASE_TS + 2)
        self.assertIs(self.stack.order(old.client_order_id).status, OrderStatus.CANCELED)

        second_attempt = self.stack.manager.replace(
            old.client_order_id, self.stack.proposal(quantity=1.0, price=101.0), timestamp=BASE_TS + 3
        )
        self.assertTrue(second_attempt.replaced)
        assert second_attempt.order is not None
        self.assertIs(second_attempt.order.status, OrderStatus.OPEN)
        self.assertNotEqual(second_attempt.order.client_order_id, old.client_order_id)
        self.assertEqual(len(self.stack.tracker.active()), 1)

    def test_replace_of_terminal_order_submits_immediately(self) -> None:
        old = self.stack.submit_order(self.stack.proposal(quantity=1.0), now_ms=BASE_TS)
        self.stack.cancel(old, now_ms=BASE_TS + 1)

        outcome = self.stack.manager.replace(
            old.client_order_id, self.stack.proposal(quantity=1.0, price=99.0), timestamp=BASE_TS + 2
        )

        self.assertTrue(outcome.replaced)
        assert outcome.order is not None
        self.assertAlmostEqual(outcome.order.price, 99.0)


if __name__ == "__main__":
    unittest.main()
