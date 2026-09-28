"""Fault：cancel race（SC-4 / SC-5）——撤单请求 ≠ 撤单成功。"""

from __future__ import annotations

import unittest

from execution.types import OrderStatus
from portfolio.types import Side
from risk.limits import RiskLimits
from tests.execution_support import ExecutionStack
from tests.support import BASE_TS


class CancelRaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExecutionStack.build(limits=RiskLimits(max_position_qty=10.0))
        self.order = self.stack.submit_order(self.stack.proposal(quantity=1.0), now_ms=BASE_TS)

    def test_sc4_cancel_request_is_not_cancel_success(self) -> None:
        self.stack.broker.defer_cancel_ack(self.order.client_order_id)

        result = self.stack.cancel(self.order, now_ms=BASE_TS + 1)

        self.assertEqual(result.updates, ())
        self.assertIs(self.stack.order(self.order.client_order_id).status, OrderStatus.PENDING_CANCEL)
        self.assertTrue(self.stack.order(self.order.client_order_id).is_active)
        # 未确认前，pending exposure 仍然占用
        self.assertAlmostEqual(self.stack.manager.open_order_exposure(), 100.0)
        # 新单不能因为「已发撤单」就被放行：exposure 仍然计入
        blocked = self.stack.submit(self.stack.proposal(quantity=1.0), now_ms=BASE_TS + 2)
        if blocked.rejected:
            self.assertIs(blocked.rejection.reason_code.value, "OPEN_ORDER_EXPOSURE_LIMIT")  # type: ignore[union-attr]

    def test_sc5_fill_during_pending_cancel_is_applied(self) -> None:
        self.stack.broker.defer_cancel_ack(self.order.client_order_id)
        self.stack.cancel(self.order, now_ms=BASE_TS + 1)

        result = self.stack.fill(self.order, quantity=0.3, timestamp=BASE_TS + 2)

        self.assertEqual([update.outcome.value for update in result.updates], ["fill_applied"])
        self.assertIs(self.stack.order(self.order.client_order_id).status, OrderStatus.PARTIALLY_FILLED)
        self.assertAlmostEqual(self.stack.accounting.position(self.stack.symbol).qty, 0.3)

        # 撤单确认随后到达：仍应进入 CANCELED（并带上最终成交量）
        self.stack.broker.cancel_ack(self.order.client_order_id, timestamp=BASE_TS + 3, executed_quantity=0.3)
        final = self.stack.poll(now_ms=BASE_TS + 3)
        self.assertEqual([update.outcome.value for update in final.updates], ["order_canceled"])
        order = self.stack.order(self.order.client_order_id)
        self.assertIs(order.status, OrderStatus.CANCELED)
        self.assertAlmostEqual(order.final_executed_quantity, 0.3)

    def test_pending_cancel_can_still_full_fill(self) -> None:
        self.stack.broker.defer_cancel_ack(self.order.client_order_id)
        self.stack.cancel(self.order, now_ms=BASE_TS + 1)

        result = self.stack.fill(self.order, quantity=1.0, timestamp=BASE_TS + 2)

        self.assertEqual([update.outcome.value for update in result.updates], ["fill_completed"])
        self.assertIs(self.stack.order(self.order.client_order_id).status, OrderStatus.FILLED)
        # 迟到的撤单确认不能把已经成交的订单改成 CANCELED
        self.stack.broker.cancel_ack(self.order.client_order_id, timestamp=BASE_TS + 3)
        late_ack = self.stack.poll(now_ms=BASE_TS + 3)
        self.assertEqual([update.outcome.value for update in late_ack.updates], ["ignored"])
        self.assertIs(self.stack.order(self.order.client_order_id).status, OrderStatus.FILLED)

    def test_cancel_reject_from_venue_keeps_order_working(self) -> None:
        self.stack.broker.drop_from_external(self.order.client_order_id)

        result = self.stack.cancel(self.order, now_ms=BASE_TS + 1)

        # 撤单被拒（未知订单）→ 本地不进入 CANCELED
        self.assertEqual([update.outcome.value for update in result.updates], ["ignored"])
        self.assertIs(self.stack.order(self.order.client_order_id).status, OrderStatus.PENDING_CANCEL)

    def test_cancel_unknown_local_order_raises(self) -> None:
        from execution.types import OrderNotFoundError

        with self.assertRaises(OrderNotFoundError):
            self.stack.cancel("probex-s1-999999", now_ms=BASE_TS)

    def test_cancel_of_terminal_order_raises(self) -> None:
        from execution.types import OrderNotFoundError

        self.stack.cancel(self.order, now_ms=BASE_TS + 1)
        with self.assertRaises(OrderNotFoundError):
            self.stack.cancel(self.order, now_ms=BASE_TS + 2)


class CancelBeforeReplaceRaceTest(unittest.TestCase):
    def test_replace_does_not_create_second_exposure(self) -> None:
        stack = ExecutionStack.build(limits=RiskLimits(max_position_qty=10.0))
        old = stack.submit_order(stack.proposal(quantity=1.0, price=100.0), now_ms=BASE_TS)
        stack.broker.defer_cancel_ack(old.client_order_id)

        first = stack.manager.replace(old.client_order_id, stack.proposal(quantity=1.0, price=101.0), timestamp=BASE_TS + 1)

        self.assertFalse(first.replaced)
        self.assertEqual(len(stack.tracker.orders), 1, "只应有旧单，没有第二份 exposure")
        self.assertEqual(len(stack.tracker.active()), 1)  # 旧单仍在 PENDING_CANCEL
        self.assertIs(stack.order(old.client_order_id).status, OrderStatus.PENDING_CANCEL)

        # 第二次 replace：旧单仍未确认终态 → 再次请求撤单（不发新单）
        second = stack.manager.replace(old.client_order_id, stack.proposal(quantity=1.0, price=102.0), timestamp=BASE_TS + 2)

        self.assertFalse(second.replaced, "旧单未确认终态前不得下新单")
        self.assertEqual(len(stack.tracker.orders), 1)
        self.assertEqual(stack.accounting.fills.count, 0)
        self.assertEqual(len(stack.broker.open_orders()), 0)

    def test_reduce_only_cancel_race(self) -> None:
        stack = ExecutionStack.build(limits=RiskLimits(max_position_qty=10.0))
        stack.accounting.record_fill(
            __import__("tests.support", fromlist=["make_fill"]).make_fill("seed", Side.BUY, 100.0, 1.0, exchange_ts=BASE_TS)
        )
        order = stack.submit_order(
            stack.proposal(side=Side.SELL, quantity=0.5, price=100.0, reduce_only=True), now_ms=BASE_TS
        )
        stack.broker.defer_cancel_ack(order.client_order_id)
        stack.cancel(order, now_ms=BASE_TS + 1)

        filled = stack.fill(order, quantity=0.5, timestamp=BASE_TS + 2)

        self.assertEqual([update.outcome.value for update in filled.updates], ["fill_completed"])
        self.assertAlmostEqual(stack.accounting.position(stack.symbol).qty, 0.5)


if __name__ == "__main__":
    unittest.main()
