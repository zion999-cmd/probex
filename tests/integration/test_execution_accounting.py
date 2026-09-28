"""Integration：成交立即入账（SC-2）+ 执行层与 Accounting 的边界。"""

from __future__ import annotations

import unittest

from execution.types import OrderStatus
from portfolio.types import Side
from risk.limits import RiskLimits
from tests.execution_support import ExecutionStack
from tests.support import BASE_TS


class ExecutionAccountingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExecutionStack.build(limits=RiskLimits(max_position_qty=10.0))

    def test_sc2_every_partial_fill_is_accounted_immediately(self) -> None:
        order = self.stack.submit_order(self.stack.proposal(quantity=1.0), now_ms=BASE_TS)

        first = self.stack.fill(order, quantity=0.2, fee=0.02, timestamp=BASE_TS + 1)
        self.assertEqual(len(first.fills), 1)
        position = self.stack.accounting.position(self.stack.symbol)
        self.assertAlmostEqual(position.qty, 0.2)  # 订单尚未完成，但已入账
        self.assertIs(self.stack.order(order.client_order_id).status, OrderStatus.PARTIALLY_FILLED)
        self.assertAlmostEqual(self.stack.accounting.trading_fees, 0.02)

        self.stack.fill(order, quantity=0.3, fee=0.03, timestamp=BASE_TS + 2)
        self.assertAlmostEqual(self.stack.accounting.position(self.stack.symbol).qty, 0.5)

        self.stack.fill(order, quantity=0.5, fee=0.05, timestamp=BASE_TS + 3)
        self.assertAlmostEqual(self.stack.accounting.position(self.stack.symbol).qty, 1.0)
        self.assertEqual(self.stack.accounting.fills.count, 3)
        self.assertAlmostEqual(self.stack.accounting.trading_fees, 0.10)

    def test_canonical_fill_carries_order_identity(self) -> None:
        order = self.stack.submit_order(self.stack.proposal(quantity=1.0, price=100.0), now_ms=BASE_TS)

        result = self.stack.fill(order, quantity=1.0, price=101.0, timestamp=BASE_TS + 5)

        fill = result.fills[0]
        self.assertEqual(fill.order_id, order.client_order_id)
        self.assertEqual(fill.symbol, self.stack.symbol)
        self.assertEqual(fill.side, Side.BUY)
        self.assertAlmostEqual(fill.price, 101.0)
        self.assertEqual(fill.exchange_ts, BASE_TS + 5)
        self.assertEqual(self.stack.accounting.fills.fills, (fill,))

    def test_closing_fill_realizes_pnl_through_accounting(self) -> None:
        buy = self.stack.submit_order(self.stack.proposal(quantity=1.0, price=100.0), now_ms=BASE_TS)
        self.stack.fill(buy, quantity=1.0, timestamp=BASE_TS + 1)
        self.stack.accounting.update_mark_price(self.stack.symbol, 110.0, timestamp=BASE_TS + 2)

        sell = self.stack.submit_order(
            self.stack.proposal(side=Side.SELL, quantity=1.0, price=110.0), now_ms=BASE_TS + 3
        )
        self.stack.fill(sell, quantity=1.0, timestamp=BASE_TS + 4)

        position = self.stack.accounting.position(self.stack.symbol)
        self.assertTrue(position.is_flat)
        self.assertAlmostEqual(position.realized_pnl, 10.0)
        self.assertAlmostEqual(self.stack.accounting.unrealized_pnl() or 0.0, 0.0)

    def test_execution_rejected_never_touches_accounting(self) -> None:
        blocked = ExecutionStack.build(limits=RiskLimits(max_position_qty=0.001))
        result = blocked.submit(blocked.proposal(quantity=1.0), now_ms=BASE_TS)

        self.assertTrue(result.rejected)
        self.assertIsNone(result.order)
        self.assertEqual(blocked.accounting.fills.count, 0)
        self.assertEqual(blocked.tracker.orders, ())
        self.assertEqual(blocked.broker.open_orders(), ())

    def test_unknown_fill_event_is_not_accounted(self) -> None:
        from execution.events import FillReceived

        result = self.stack.engine.on_events(
            (FillReceived("probex-s1-999999", "e1", "t1", 100.0, 1.0, BASE_TS),), now_ms=BASE_TS
        )

        self.assertEqual(result.fills, ())
        self.assertEqual(self.stack.accounting.fills.count, 0)
        self.assertEqual([update.outcome.value for update in result.updates], ["unknown_order"])

    def test_accounting_dedupe_is_the_second_line_of_defence(self) -> None:
        """即使 execution 层漏掉去重，Accounting 的 FillLedger 也不会二次记账。"""
        order = self.stack.submit_order(self.stack.proposal(quantity=1.0), now_ms=BASE_TS)
        first = self.stack.fill(order, quantity=0.5, timestamp=BASE_TS + 1)
        fill = first.fills[0]

        application = self.stack.accounting.record_fill(fill)

        self.assertFalse(application.accepted)
        self.assertAlmostEqual(self.stack.accounting.position(self.stack.symbol).qty, 0.5)
        self.assertEqual(self.stack.accounting.fills.duplicate_count, 1)


if __name__ == "__main__":
    unittest.main()
