"""Replay：同一 ExecutionEvent 序列重放两次必须完全一致（SC-17）。"""

from __future__ import annotations

import unittest

from execution.types import OrderStatus
from portfolio.types import Side
from risk.limits import RiskLimits
from tests.execution_support import ExecutionStack
from tests.support import BASE_TS


def _replay() -> tuple:
    """跑一段包含 partial fill / cancel race / late fill / 重复投递 / reconcile 的脚本。"""
    stack = ExecutionStack.build(
        limits=RiskLimits(max_position_qty=5.0, max_open_order_exposure=500.0), session="s1"
    )

    first = stack.submit_order(stack.proposal(quantity=1.0, price=100.0), now_ms=BASE_TS)
    stack.fill(first, quantity=0.2, fee=0.02, timestamp=BASE_TS + 1)
    stack.fill(first, quantity=0.3, fee=0.03, timestamp=BASE_TS + 2)
    stack.broker.defer_cancel_ack(first.client_order_id)
    stack.cancel(first, now_ms=BASE_TS + 3)
    stack.fill(first, quantity=0.2, fee=0.02, timestamp=BASE_TS + 4)  # PENDING_CANCEL 期间成交
    stack.broker.cancel_ack(first.client_order_id, timestamp=BASE_TS + 5, executed_quantity=0.7)
    stack.poll(now_ms=BASE_TS + 5)
    stack.fill(first, quantity=0.2, fee=0.02, timestamp=BASE_TS + 4)  # late fill（窗口内）

    second = stack.submit_order(stack.proposal(side=Side.SELL, quantity=0.5, price=101.0), now_ms=BASE_TS + 6)
    execution_id = stack.broker.fill(second.client_order_id, quantity=0.5, price=101.0, timestamp=BASE_TS + 7)
    stack.poll(now_ms=BASE_TS + 7)
    stack.broker.duplicate_fill(
        second.client_order_id,
        execution_id=execution_id,
        trade_id="t-dup",
        quantity=0.5,
        price=101.0,
        timestamp=BASE_TS + 8,
    )
    stack.poll(now_ms=BASE_TS + 8)
    stack.reconcile(timestamp=BASE_TS + 9)

    return (
        stack.state(),
        tuple(stack.order(order.client_order_id).status for order in stack.tracker.orders),
        (
            stack.tracker.applied_fill_count,
            stack.tracker.duplicate_fill_count,
            stack.tracker.late_fill_count,
            stack.tracker.rejected_fill_count,
        ),
        stack.accounting.fills.duplicate_count,
    )


class ExecutionDeterminismTest(unittest.TestCase):
    def test_sc17_two_replays_are_field_identical(self) -> None:
        first = _replay()
        second = _replay()

        self.assertEqual(first, second)

    def test_replay_reaches_the_expected_final_state(self) -> None:
        state, statuses, counters, accounting_duplicates = _replay()
        orders, fills, position, balance, equity, exposure = state

        self.assertEqual(len(orders), 2)
        self.assertEqual(statuses[0], OrderStatus.CANCELED)  # 先撤后到 late fill：终态不变
        self.assertEqual(statuses[1], OrderStatus.FILLED)
        self.assertEqual(len(fills), 5)  # 0.2 + 0.3 + 0.2 + late 0.2 + 0.5
        self.assertAlmostEqual(exposure, 0.0)
        # reconcile 会重放外部成交历史 → 全部被 execution 层去重（这就是「第二道防线之前的第一道」）
        self.assertGreaterEqual(counters[1], 1)  # duplicate fill（含 reconcile 重放）
        self.assertEqual(counters[2], 1)  # late fill
        self.assertEqual(counters[3], 0)  # 无被拒成交
        self.assertEqual(accounting_duplicates, 0)
        # long 0.7（0.2+0.3+0.2）+ late 0.2 = 0.9 long；再 sell 0.5 → 净 0.4 long
        self.assertAlmostEqual(position[0], 0.4, places=9)  # type: ignore[index]
        self.assertEqual(len(fills), 5)
        assert isinstance(balance, float) and isinstance(equity, float)

    def test_replay_is_stable_across_several_runs(self) -> None:
        states = [_replay() for _ in range(3)]
        self.assertEqual(states[0], states[1])
        self.assertEqual(states[1], states[2])

    def test_replay_has_no_wall_clock_dependency(self) -> None:
        import inspect

        import execution.engine as engine_module
        import execution.tracker as tracker_module

        for module in (engine_module, tracker_module):
            with self.subTest(module=module.__name__):
                source = inspect.getsource(module)
                self.assertNotIn("time.time", source)
                self.assertNotIn("datetime", source)


if __name__ == "__main__":
    unittest.main()
