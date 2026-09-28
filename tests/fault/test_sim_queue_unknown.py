"""P0001.8 故障测试：L2 变化不是成交证据（SC-2）、无法观察价位不得猜测成交（SC-9）。"""

from __future__ import annotations

import unittest

from execution.simulation.types import QueueState
from risk.limits import RiskLimits
from tests.sim_support import SimStack, book_delta, book_snapshot, sell_aggressor, ts


class L2DecreaseIsNotFillEvidenceTest(unittest.TestCase):
    def _stack(self) -> SimStack:
        stack = SimStack.build(limits=RiskLimits(max_position_qty=10.0))
        stack.feed([book_snapshot(update_id=1, bids=((100.0, 3.0),), asks=((101.0, 2.0),), offset=0)])
        return stack

    def test_sc2_level_size_reduction_does_not_advance_the_queue(self) -> None:
        stack = self._stack()
        order = stack.submit_order(stack.proposal(quantity=1.0, price=100.0), now_ms=ts(0))

        stack.feed([book_delta(2, 2, bids=((100.0, 0.5),), offset=1)])  # 可见量 3.0 → 0.5

        view = stack.venue.order_view(order.client_order_id)
        self.assertEqual(view.queue_ahead, 3.0)  # 完全不变
        self.assertEqual(stack.venue.fills, ())

    def test_sc2_level_removal_does_not_advance_the_queue(self) -> None:
        stack = self._stack()
        order = stack.submit_order(stack.proposal(quantity=1.0, price=100.0), now_ms=ts(0))

        stack.feed([book_delta(2, 2, bids=((100.0, 0.0),), offset=1)])  # 删除档位

        view = stack.venue.order_view(order.client_order_id)
        self.assertEqual(view.queue_ahead, 3.0)
        self.assertEqual(stack.venue.fills, ())

    def test_only_trades_can_consume_the_queue_after_a_decrease(self) -> None:
        stack = self._stack()
        order = stack.submit_order(stack.proposal(quantity=1.0, price=100.0), now_ms=ts(0))
        stack.feed([book_delta(2, 2, bids=((100.0, 0.5),), offset=1)])

        stack.feed([sell_aggressor(1, price=100.0, quantity=3.0, offset=2)])
        after_queue = stack.venue.order_view(order.client_order_id)
        stack.feed([sell_aggressor(2, price=100.0, quantity=0.2, offset=3)])

        self.assertEqual(after_queue.queue_ahead, 0.0)
        self.assertAlmostEqual(stack.order(order.client_order_id).filled_quantity, 0.2)


class QueueUnknownTest(unittest.TestCase):
    def test_sc9_price_outside_recorded_depth_is_unknown_and_never_fills(self) -> None:
        stack = SimStack.build()
        stack.feed([book_snapshot(update_id=1, bids=((100.0, 3.0),), asks=((101.0, 2.0),), offset=0)])
        order = stack.submit_order(stack.proposal(quantity=1.0, price=99.0), now_ms=ts(0))

        stack.feed(
            [
                sell_aggressor(1, price=99.0, quantity=5.0, offset=1),
                sell_aggressor(2, price=98.5, quantity=5.0, offset=2),
            ]
        )

        view = stack.venue.order_view(order.client_order_id)
        self.assertIs(view.queue_state, QueueState.UNKNOWN)
        self.assertIsNone(view.queue_ahead)
        self.assertIsNone(view.initial_queue_ahead)
        self.assertEqual(view.queue_rebuild_count, 0)
        self.assertEqual(stack.venue.fills, ())
        self.assertAlmostEqual(stack.order(order.client_order_id).filled_quantity, 0.0)

    def test_unknown_queue_does_not_mean_zero(self) -> None:
        """等价性检查：`UNKNOWN` 不是 `ahead == 0`（否则第一笔成交就会立刻打满）。"""
        stack = SimStack.build()
        stack.feed([book_snapshot(update_id=1, bids=((100.0, 3.0),), asks=((101.0, 2.0),), offset=0)])
        order = stack.submit_order(stack.proposal(quantity=1.0, price=99.0), now_ms=ts(0))

        stack.feed([sell_aggressor(1, price=99.0, quantity=1.0, offset=1)])

        self.assertAlmostEqual(stack.order(order.client_order_id).filled_quantity, 0.0)

    def test_queue_is_established_once_the_level_becomes_observable(self) -> None:
        stack = SimStack.build()
        stack.feed([book_snapshot(update_id=1, bids=((100.0, 3.0),), asks=((101.0, 2.0),), offset=0)])
        order = stack.submit_order(stack.proposal(quantity=1.0, price=99.0), now_ms=ts(0))
        stack.feed([sell_aggressor(1, price=99.0, quantity=1.0, offset=1)])

        stack.feed([book_delta(2, 2, bids=((99.0, 2.0),), offset=2)])  # 该价位出现
        established = stack.venue.order_view(order.client_order_id)
        stack.feed([sell_aggressor(2, price=99.0, quantity=2.0, offset=3)])
        after_queue = stack.venue.order_view(order.client_order_id)
        stack.feed([sell_aggressor(3, price=99.0, quantity=0.4, offset=4)])

        self.assertIs(established.queue_state, QueueState.KNOWN)
        self.assertEqual(established.queue_ahead, 2.0)
        self.assertEqual(after_queue.queue_ahead, 0.0)
        self.assertAlmostEqual(stack.order(order.client_order_id).filled_quantity, 0.4)

    def test_established_queue_survives_later_level_removal(self) -> None:
        """队列估计一旦建立，就由成交推进；档位消失不改变已建立的估计（也不是成交证据）。"""
        stack = SimStack.build()
        stack.feed([book_snapshot(update_id=1, bids=((100.0, 3.0),), asks=((101.0, 2.0),), offset=0)])
        order = stack.submit_order(stack.proposal(quantity=1.0, price=100.0), now_ms=ts(0))

        stack.feed([book_delta(2, 2, bids=((100.0, 0.0),), offset=1)])

        view = stack.venue.order_view(order.client_order_id)
        self.assertIs(view.queue_state, QueueState.KNOWN)
        self.assertEqual(view.queue_ahead, 3.0)
        self.assertEqual(stack.venue.fills, ())


if __name__ == "__main__":
    unittest.main()
