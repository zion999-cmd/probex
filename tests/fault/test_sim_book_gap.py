"""P0001.8 故障测试：Book gap / unhealthy 时的成交推断挂起与队列重建（SC-7 / SC-8）。

`BookHealth != HEALTHY` 时不得继续推导 Maker 成交；恢复后旧 queue estimate **作废**，
必须重建（或保持 UNKNOWN），不能无声续算。
"""

from __future__ import annotations

import unittest

from execution.simulation.types import FillInferenceState, QueueState
from market.health.state import BookHealth
from risk.limits import RiskLimits
from tests.sim_support import SimStack, book_delta, book_snapshot, sell_aggressor, ts
from tests.support import SYMBOL


class SimBookGapTest(unittest.TestCase):
    def _stack_with_resting_order(self) -> tuple[SimStack, str]:
        stack = SimStack.build(limits=RiskLimits(max_position_qty=10.0))
        stack.feed([book_snapshot(update_id=1, bids=((100.0, 3.0),), offset=0)])
        order = stack.submit_order(stack.proposal(quantity=1.0, price=100.0), now_ms=ts(0))
        stack.feed([sell_aggressor(1, price=100.0, quantity=1.0, offset=2)])  # 队列 3 → 2
        return stack, order.client_order_id

    def test_sc7_gap_suspends_fill_inference(self) -> None:
        stack, order_id = self._stack_with_resting_order()

        stack.feed([book_delta(50, 50, bids=((100.0, 3.0),), offset=3)])

        view = stack.venue.order_view(order_id)
        self.assertIs(stack.venue.book.health, BookHealth.STALE)
        self.assertIs(view.fill_inference, FillInferenceState.SUSPENDED)
        self.assertIs(view.queue_state, QueueState.UNKNOWN)
        self.assertIsNone(view.queue_ahead)

    def test_sc7_no_fill_is_inferred_while_unhealthy(self) -> None:
        stack, order_id = self._stack_with_resting_order()
        stack.feed([book_delta(50, 50, bids=((100.0, 3.0),), offset=3)])

        stack.feed(
            [
                sell_aggressor(2, price=100.0, quantity=99.0, offset=4),
                sell_aggressor(3, price=99.5, quantity=99.0, offset=5),
            ]
        )

        self.assertEqual(stack.venue.fills, ())
        self.assertAlmostEqual(stack.order(order_id).filled_quantity, 0.0)

    def test_sc8_recovery_rebuilds_the_queue_from_the_new_book(self) -> None:
        stack, order_id = self._stack_with_resting_order()
        stack.feed([book_delta(50, 50, bids=((100.0, 3.0),), offset=3)])
        stack.feed([sell_aggressor(2, price=100.0, quantity=99.0, offset=4)])

        stack.feed([book_snapshot(update_id=50, bids=((100.0, 4.0),), asks=((101.0, 2.0),), offset=5)])

        view = stack.venue.order_view(order_id)
        self.assertIs(stack.venue.book.health, BookHealth.HEALTHY)
        self.assertIs(view.fill_inference, FillInferenceState.ACTIVE)
        self.assertIs(view.queue_state, QueueState.KNOWN)
        self.assertEqual(view.queue_ahead, 4.0)  # 用恢复后的可见数量重建
        self.assertEqual(view.initial_queue_ahead, 3.0)  # 初始估计被保留供审计
        self.assertEqual(view.queue_rebuild_count, 1)

    def test_sc8_old_queue_is_not_silently_reused(self) -> None:
        stack, order_id = self._stack_with_resting_order()
        stack.feed([book_delta(50, 50, bids=((100.0, 3.0),), offset=3)])
        stack.feed([book_snapshot(update_id=50, bids=((100.0, 4.0),), asks=((101.0, 2.0),), offset=5)])

        # gap 之前的剩余队列是 2.0：若被无声续用，这笔 4.0 的成交会立刻打满订单
        stack.feed([sell_aggressor(3, price=100.0, quantity=4.0, offset=6)])
        after_queue = stack.venue.order_view(order_id)

        self.assertEqual(stack.venue.fills, ())
        self.assertEqual(after_queue.queue_ahead, 0.0)

        stack.feed([sell_aggressor(4, price=100.0, quantity=0.5, offset=7)])

        self.assertAlmostEqual(stack.order(order_id).filled_quantity, 0.5)

    def test_queue_state_is_unknown_without_any_healthy_book(self) -> None:
        stack = SimStack.build()
        order = stack.submit_order(stack.proposal(quantity=1.0, price=100.0), now_ms=ts(0))

        stack.feed([sell_aggressor(1, price=100.0, quantity=99.0, offset=1)])

        view = stack.venue.order_view(order.client_order_id)
        self.assertIs(view.queue_state, QueueState.UNKNOWN)
        self.assertEqual(stack.venue.fills, ())
        self.assertIs(stack.venue.book.health, BookHealth.AWAITING_SNAPSHOT)

    def test_fill_evidence_records_the_ordinal_and_reason(self) -> None:
        stack, order_id = self._stack_with_resting_order()

        stack.feed([sell_aggressor(2, price=100.0, quantity=5.0, offset=9)])

        fill = stack.venue.fills[0]
        self.assertEqual(fill.event_ordinal, 2)
        self.assertEqual(fill.exchange_ts, ts(9))
        self.assertEqual(fill.queue_state, QueueState.KNOWN)
        self.assertEqual(fill.fill_reason.value, "queue_consumed")
        self.assertEqual(fill.exchange_order_id, stack.venue.order_view(order_id).exchange_order_id)
        self.assertEqual(stack.accounting.position(SYMBOL).qty, 1.0)


if __name__ == "__main__":
    unittest.main()
