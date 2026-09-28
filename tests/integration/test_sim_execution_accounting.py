"""P0001.8 集成测试：模拟成交通过 OrderTracker → Accounting 正常记账（SC-13）。

链路：SimulatedVenue → FillReceived → OrderTracker（canonical Fill）→ AccountingCore。
"""

from __future__ import annotations

import unittest

from execution.types import OrderStatus
from portfolio.types import Side
from tests.sim_support import DEFAULT_TEST_FEE_RATE, SimStack, book_snapshot, buy_aggressor, sell_aggressor, ts
from tests.support import SYMBOL


class SimulatedExecutionAccountingTest(unittest.TestCase):
    def _rest(self, stack: SimStack, *, side: Side = Side.BUY, quantity: float = 1.0, price: float = 100.0):
        stack.feed([book_snapshot(offset=0)])
        return stack.submit_order(stack.proposal(side=side, quantity=quantity, price=price), now_ms=ts(0))

    def test_sc13_partial_fills_accumulate_into_one_position(self) -> None:
        stack = SimStack.build()
        order = self._rest(stack)

        stack.feed(
            [
                sell_aggressor(1, price=100.0, quantity=3.0, offset=2),  # 清空队列，不成交
                sell_aggressor(2, price=100.0, quantity=0.25, offset=3),
                sell_aggressor(3, price=100.0, quantity=0.75, offset=4),
            ]
        )

        position = stack.accounting.position(SYMBOL)
        self.assertAlmostEqual(position.qty, 1.0)
        self.assertAlmostEqual(position.avg_entry_price, 100.0)
        self.assertIs(stack.order(order.client_order_id).status, OrderStatus.FILLED)
        self.assertAlmostEqual(stack.accounting.trading_fees, 100.0 * 1.0 * DEFAULT_TEST_FEE_RATE)

    def test_sc13_canonical_fills_flow_into_the_ledger(self) -> None:
        stack = SimStack.build()
        order = self._rest(stack, quantity=0.5)

        stack.feed([sell_aggressor(7, price=100.0, quantity=10.0, offset=2)])

        fills = stack.accounting.fills.fills
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].order_id, order.client_order_id)
        self.assertEqual(fills[0].fill_id, stack.venue.fills[0].execution_id)
        self.assertEqual(fills[0].trade_id, stack.venue.fills[0].trade_id)
        self.assertEqual(fills[0].fee_asset, "USDT")
        self.assertEqual(fills[0].side, Side.BUY)
        self.assertAlmostEqual(fills[0].quantity, 0.5)

    def test_duplicate_trade_delivery_does_not_double_account(self) -> None:
        stack = SimStack.build()
        order = self._rest(stack, quantity=0.5)

        stack.feed(
            [
                sell_aggressor(1, price=100.0, quantity=10.0, offset=2),
                sell_aggressor(1, price=100.0, quantity=10.0, offset=3),  # 重复投递同一笔
            ]
        )

        self.assertAlmostEqual(stack.accounting.position(SYMBOL).qty, 0.5)
        self.assertEqual(len(stack.accounting.fills.fills), 1)
        self.assertEqual(stack.venue.duplicate_trade_count, 1)
        self.assertIs(stack.order(order.client_order_id).status, OrderStatus.FILLED)

    def test_balance_reflects_only_fees_while_the_position_is_open(self) -> None:
        stack = SimStack.build(mark=100.0)
        self._rest(stack, quantity=0.5)

        stack.feed([sell_aggressor(1, price=100.0, quantity=10.0, offset=2)])

        fee = 100.0 * 0.5 * DEFAULT_TEST_FEE_RATE
        self.assertAlmostEqual(stack.accounting.balance, 10_000.0 - fee)
        self.assertAlmostEqual(stack.accounting.equity() or 0.0, 10_000.0 - fee)

    def test_two_sided_fills_net_into_one_position(self) -> None:
        stack = SimStack.build()
        buy = self._rest(stack, side=Side.BUY, quantity=1.0, price=100.0)
        stack.feed([sell_aggressor(1, price=100.0, quantity=10.0, offset=2)])
        sell = stack.submit_order(stack.proposal(side=Side.SELL, quantity=0.4, price=101.0), now_ms=ts(3))
        stack.feed([buy_aggressor(2, price=101.0, quantity=10.0, offset=4)])

        position = stack.accounting.position(SYMBOL)
        self.assertAlmostEqual(position.qty, 0.6)
        self.assertAlmostEqual(position.avg_entry_price, 100.0)  # 平仓不动均价
        self.assertIs(stack.order(buy.client_order_id).status, OrderStatus.FILLED)
        self.assertIs(stack.order(sell.client_order_id).status, OrderStatus.FILLED)

    def test_unfilled_order_keeps_exposure_visible_to_risk(self) -> None:
        stack = SimStack.build()
        self._rest(stack, quantity=1.0, price=100.0)

        snapshot = stack.engine.snapshot(SYMBOL, now_ms=ts(1))

        self.assertAlmostEqual(snapshot.open_order_exposure, 100.0)
        self.assertAlmostEqual(stack.accounting.position(SYMBOL).qty, 0.0)

    def test_sc13_reconciliation_of_a_simulated_fill_is_a_no_op(self) -> None:
        """模拟 venue 的外部事实与本地一致：reconcile 不得产生纠正动作，也不得重复记账。"""
        stack = SimStack.build()
        order = self._rest(stack, quantity=0.5)
        stack.feed([sell_aggressor(1, price=100.0, quantity=10.0, offset=2)])

        report = stack.reconcile()

        self.assertEqual(report.corrective_actions, ())
        self.assertEqual(len(stack.accounting.fills.fills), 1)
        self.assertAlmostEqual(stack.accounting.position(SYMBOL).qty, 0.5)
        self.assertIs(stack.order(order.client_order_id).status, OrderStatus.FILLED)
        self.assertEqual(stack.venue.open_orders(), ())


if __name__ == "__main__":
    unittest.main()
