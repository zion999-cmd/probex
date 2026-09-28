"""Fault：late fill（SC-6）——终态之后到达的合法成交不能丢。"""

from __future__ import annotations

import unittest

from execution.types import OrderStatus
from risk.limits import RiskLimits
from tests.execution_support import ExecutionStack
from tests.support import BASE_TS


class LateFillTest(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExecutionStack.build(limits=RiskLimits(max_position_qty=10.0))

    def test_sc6_fill_after_cancel_confirmation_is_accounted(self) -> None:
        order = self.stack.submit_order(self.stack.proposal(quantity=1.0), now_ms=BASE_TS)
        self.stack.fill(order, quantity=0.5, fee=0.05, timestamp=BASE_TS + 1)
        self.stack.cancel(order, now_ms=BASE_TS + 5)

        self.assertIs(self.stack.order(order.client_order_id).status, OrderStatus.CANCELED)
        self.assertAlmostEqual(self.stack.accounting.position(self.stack.symbol).qty, 0.5)

        late = self.stack.fill(order, quantity=0.3, fee=0.03, timestamp=BASE_TS + 4)

        self.assertEqual([update.outcome.value for update in late.updates], ["late_fill_applied"])
        self.assertEqual(len(late.fills), 1)

        final = self.stack.order(order.client_order_id)
        self.assertIs(final.status, OrderStatus.CANCELED)  # 终态不被 late fill 改写
        self.assertAlmostEqual(final.final_executed_quantity, 0.8)
        self.assertAlmostEqual(final.executed_quantity, 0.8)
        self.assertAlmostEqual(self.stack.accounting.position(self.stack.symbol).qty, 0.8)
        self.assertEqual(self.stack.accounting.fills.count, 2)
        self.assertEqual(self.stack.tracker.late_fill_count, 1)

    def test_late_fill_after_expiry(self) -> None:
        order = self.stack.submit_order(self.stack.proposal(quantity=1.0), now_ms=BASE_TS)
        self.stack.broker.status_update(order.client_order_id, status=OrderStatus.EXPIRED, timestamp=BASE_TS + 5)
        self.stack.poll(now_ms=BASE_TS + 5)
        self.assertIs(self.stack.order(order.client_order_id).status, OrderStatus.EXPIRED)

        late = self.stack.fill(order, quantity=1.0, timestamp=BASE_TS + 4)

        self.assertEqual([update.outcome.value for update in late.updates], ["late_fill_applied"])
        self.assertIs(self.stack.order(order.client_order_id).status, OrderStatus.EXPIRED)
        self.assertAlmostEqual(self.stack.accounting.position(self.stack.symbol).qty, 1.0)

    def test_fill_after_the_validity_window_is_rejected(self) -> None:
        order = self.stack.submit_order(self.stack.proposal(quantity=1.0), now_ms=BASE_TS)
        self.stack.cancel(order, now_ms=BASE_TS + 5)

        result = self.stack.fill(order, quantity=0.4, timestamp=BASE_TS + 6)

        self.assertEqual([update.outcome.value for update in result.updates], ["fill_rejected"])
        self.assertIn("validity window", result.updates[0].detail)
        self.assertEqual(result.fills, ())
        self.assertEqual(self.stack.accounting.fills.count, 0)
        self.assertAlmostEqual(self.stack.order(order.client_order_id).executed_quantity, 0.0)

    def test_late_fill_never_resurrects_order_status(self) -> None:
        order = self.stack.submit_order(self.stack.proposal(quantity=1.0), now_ms=BASE_TS)
        self.stack.fill(order, quantity=0.2, timestamp=BASE_TS + 1)
        self.stack.cancel(order, now_ms=BASE_TS + 2)

        self.stack.fill(order, quantity=0.2, timestamp=BASE_TS + 2)

        self.assertIs(self.stack.order(order.client_order_id).status, OrderStatus.CANCELED)
        self.assertEqual(len(self.stack.tracker.active()), 0)
        self.assertEqual(self.stack.manager.open_order_exposure(), 0.0)

    def test_duplicate_late_fill_is_deduped(self) -> None:
        order = self.stack.submit_order(self.stack.proposal(quantity=1.0), now_ms=BASE_TS)
        self.stack.cancel(order, now_ms=BASE_TS + 5)
        execution_id = self.stack.broker.fill(
            order.client_order_id, quantity=0.3, price=100.0, timestamp=BASE_TS + 4
        )
        self.stack.poll(now_ms=BASE_TS + 4)

        self.stack.broker.duplicate_fill(
            order.client_order_id,
            execution_id=execution_id,
            trade_id="t-dup",
            quantity=0.3,
            price=100.0,
            timestamp=BASE_TS + 4,
        )
        duplicate = self.stack.poll(now_ms=BASE_TS + 4)

        self.assertEqual([update.outcome.value for update in duplicate.updates], ["fill_duplicate"])
        self.assertEqual(self.stack.accounting.fills.count, 1)
        self.assertAlmostEqual(self.stack.order(order.client_order_id).executed_quantity, 0.3)


if __name__ == "__main__":
    unittest.main()
