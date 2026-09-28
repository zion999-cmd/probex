"""Fault：LOST 订单经 reconciliation 恢复真实状态（SC-14）。"""

from __future__ import annotations

import unittest

from execution.types import OrderStatus
from risk.limits import RiskLimits
from tests.execution_support import ExecutionStack
from tests.support import BASE_TS


class LostOrderTest(unittest.TestCase):
    def test_ack_never_arrived_then_restored_by_reconciliation(self) -> None:
        stack = ExecutionStack.build(limits=RiskLimits(max_position_qty=10.0))
        stack.broker.drop_next_ack()
        result = stack.submit(stack.proposal(quantity=1.0), now_ms=BASE_TS)
        order = stack.order(result.order.client_order_id)  # type: ignore[union-attr]

        self.assertIs(order.status, OrderStatus.PENDING_CREATE)
        stuck = stack.tracker.pending_unacked(now_ms=BASE_TS + 2_000, timeout_ms=1_000)
        self.assertEqual([item.client_order_id for item in stuck], [order.client_order_id])

        # 超时未确认 → 标记 LOST（不是终态）
        stack.tracker.mark_lost(order.client_order_id, timestamp=BASE_TS + 2_000, reason="ack timeout")
        self.assertTrue(stack.order(order.client_order_id).is_lost)
        self.assertEqual(len(stack.tracker.lost()), 1)

        # 交易所实际上有这个订单 → reconciliation 恢复真实状态
        report = stack.reconcile(timestamp=BASE_TS + 2_100)

        self.assertIn("RESTORED", [kind.name for kind in report.kinds()])
        self.assertFalse(report.converged)
        restored = stack.order(order.client_order_id)
        self.assertIs(restored.status, OrderStatus.OPEN)
        self.assertEqual(restored.exchange_order_id, stack.broker.open_orders()[0].exchange_order_id)

    def test_local_open_missing_externally_becomes_lost(self) -> None:
        stack = ExecutionStack.build(limits=RiskLimits(max_position_qty=10.0))
        order = stack.submit_order(stack.proposal(quantity=1.0), now_ms=BASE_TS)
        stack.broker.drop_from_external(order.client_order_id)

        report = stack.reconcile(timestamp=BASE_TS + 100)

        self.assertIn("MARKED_LOST", [kind.name for kind in report.kinds()])
        self.assertTrue(stack.order(order.client_order_id).is_lost)
        self.assertFalse(report.converged)

        # 第二次 reconcile：LOST 是已确认事实，没有可纠正项
        second = stack.reconcile(timestamp=BASE_TS + 200)
        self.assertTrue(second.converged)
        self.assertEqual(second.corrective_actions, ())

    def test_lost_order_exposure_moves_to_uncertain(self) -> None:
        """P0001.6.1 取代了 P0001.6 的「LOST 不计入暴露」：LOST 属于 uncertain，仍占用风险额度。"""
        stack = ExecutionStack.build(limits=RiskLimits(max_position_qty=10.0, max_open_order_exposure=100.0))
        order = stack.submit_order(stack.proposal(quantity=1.0, price=100.0), now_ms=BASE_TS)
        stack.tracker.mark_lost(order.client_order_id, timestamp=BASE_TS + 1, reason="manual")

        self.assertEqual(stack.tracker.active(), ())
        self.assertEqual(stack.manager.uncertain_exposure, 100.0)
        self.assertEqual(stack.manager.confirmed_open_exposure, 0.0)
        self.assertEqual(stack.manager.open_order_exposure(), 100.0)

        # 仍然会挡住新增暴露（SC-2）
        blocked = stack.submit(stack.proposal(quantity=0.5, price=100.0), now_ms=BASE_TS + 2)
        self.assertTrue(blocked.rejected)
        self.assertIs(blocked.rejection.reason_code.value, "OPEN_ORDER_EXPOSURE_LIMIT")  # type: ignore[union-attr]

    def test_terminal_orders_cannot_become_lost(self) -> None:
        from execution.types import IllegalOrderTransition

        stack = ExecutionStack.build(limits=RiskLimits(max_position_qty=10.0))
        order = stack.submit_order(stack.proposal(quantity=1.0), now_ms=BASE_TS)
        stack.cancel(order, now_ms=BASE_TS + 1)

        with self.assertRaises(IllegalOrderTransition):
            stack.tracker.mark_lost(order.client_order_id, timestamp=BASE_TS + 2, reason="nope")

    def test_lost_order_can_still_receive_fills_after_restore(self) -> None:
        stack = ExecutionStack.build(limits=RiskLimits(max_position_qty=10.0))
        order = stack.submit_order(stack.proposal(quantity=1.0), now_ms=BASE_TS)
        stack.tracker.mark_lost(order.client_order_id, timestamp=BASE_TS + 1, reason="manual")
        stack.reconcile(timestamp=BASE_TS + 2)

        result = stack.fill(order, quantity=0.4, timestamp=BASE_TS + 3)

        self.assertEqual([update.outcome.value for update in result.updates], ["fill_applied"])
        self.assertAlmostEqual(stack.accounting.position(stack.symbol).qty, 0.4)


if __name__ == "__main__":
    unittest.main()
