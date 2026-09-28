"""Fault：重复执行事件不得二次影响 Order 与 Accounting（SC-3）。"""

from __future__ import annotations

import unittest

from risk.limits import RiskLimits
from tests.execution_support import ExecutionStack
from tests.support import BASE_TS


class DuplicateExecutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExecutionStack.build(limits=RiskLimits(max_position_qty=10.0))
        self.order = self.stack.submit_order(self.stack.proposal(quantity=1.0), now_ms=BASE_TS)

    def test_sc3_duplicate_fill_does_not_touch_order_or_accounting(self) -> None:
        execution_id = self.stack.broker.fill(
            self.order.client_order_id, quantity=0.4, price=100.0, fee=0.04, timestamp=BASE_TS + 1
        )
        first = self.stack.poll(now_ms=BASE_TS + 1)
        self.assertEqual(len(first.fills), 1)

        self.stack.broker.duplicate_fill(
            self.order.client_order_id,
            execution_id=execution_id,
            trade_id="t-dup",
            quantity=0.4,
            price=100.0,
            timestamp=BASE_TS + 2,
        )
        second = self.stack.poll(now_ms=BASE_TS + 2)

        self.assertEqual([update.outcome.value for update in second.updates], ["fill_duplicate"])
        self.assertEqual(second.fills, ())
        self.assertAlmostEqual(self.stack.order(self.order.client_order_id).filled_quantity, 0.4)
        self.assertAlmostEqual(self.stack.accounting.position(self.stack.symbol).qty, 0.4)
        self.assertAlmostEqual(self.stack.accounting.trading_fees, 0.04)
        self.assertEqual(self.stack.accounting.fills.count, 1)

    def test_duplicate_trade_id_is_deduped_across_events(self) -> None:
        self.stack.broker.fill(
            self.order.client_order_id, quantity=0.2, price=100.0, timestamp=BASE_TS + 1, trade_id="t-shared"
        )
        self.stack.poll(now_ms=BASE_TS + 1)

        self.stack.broker.duplicate_fill(
            self.order.client_order_id,
            execution_id="exec-other",
            trade_id="t-shared",
            quantity=0.2,
            price=100.0,
            timestamp=BASE_TS + 2,
        )
        result = self.stack.poll(now_ms=BASE_TS + 2)

        self.assertEqual([update.outcome.value for update in result.updates], ["fill_duplicate"])
        self.assertAlmostEqual(self.stack.accounting.position(self.stack.symbol).qty, 0.2)

    def test_duplicate_cancel_ack_is_ignored(self) -> None:
        self.stack.cancel(self.order, now_ms=BASE_TS + 1)

        self.stack.broker.cancel_ack(self.order.client_order_id, timestamp=BASE_TS + 2)
        result = self.stack.poll(now_ms=BASE_TS + 2)

        self.assertEqual([update.outcome.value for update in result.updates], ["ignored"])

    def test_duplicate_accept_event_is_ignored(self) -> None:
        from execution.events import OrderAccepted

        result = self.stack.engine.on_events(
            (OrderAccepted(self.order.client_order_id, "paper-000001", BASE_TS + 1),), now_ms=BASE_TS + 1
        )

        self.assertEqual([update.outcome.value for update in result.updates], ["ignored"])
        self.assertEqual(self.stack.order(self.order.client_order_id).exchange_order_id, "paper-000001")

    def test_two_layer_dedupe(self) -> None:
        """同一次成交被投递两次：execution 层拦住，Accounting 层仍保留兜底。"""
        execution_id = self.stack.broker.fill(
            self.order.client_order_id, quantity=0.5, price=100.0, timestamp=BASE_TS + 1
        )
        applied = self.stack.poll(now_ms=BASE_TS + 1)
        canonical = applied.fills[0]

        # 手工把同一个 canonical Fill 再喂给 Accounting（模拟 execution 层漏网）
        second_layer = self.stack.accounting.record_fill(canonical)

        self.assertFalse(second_layer.accepted)
        self.assertAlmostEqual(self.stack.accounting.position(self.stack.symbol).qty, 0.5)
        self.assertEqual(self.stack.accounting.fills.count, 1)
        self.assertEqual(self.stack.accounting.fills.duplicate_count, 1)
        self.assertAlmostEqual(self.stack.tracker.duplicate_fill_count, 0.0)  # execution 层未看到重复
        self.assertEqual(self.stack.broker.recent_fills()[0].execution_id, execution_id)


if __name__ == "__main__":
    unittest.main()
