"""P0001.8 故障测试：撤单延迟窗口内的成交与生效后的封停（SC-11）。

复用 P0001.6 的 cancel-race 语义：

```text
cancel requested --(cancel_latency)--> cancel effective
期间订单仍可成交；生效之后禁止任何新成交；同刻平局时撤单优先。
```
"""

from __future__ import annotations

import unittest

from execution.types import OrderStatus
from risk.limits import RiskLimits
from tests.sim_support import SimStack, book_delta, book_snapshot, sell_aggressor, ts

CANCEL_LATENCY_MS = 40


class SimCancelRaceTest(unittest.TestCase):
    def _stack(self) -> SimStack:
        stack = SimStack.build(limits=RiskLimits(max_position_qty=10.0), cancel_latency_ms=CANCEL_LATENCY_MS)
        stack.feed([book_snapshot(update_id=1, bids=((100.0, 3.0),), asks=((101.0, 2.0),), offset=0)])
        return stack

    def test_sc11_fill_during_the_cancel_window_is_applied(self) -> None:
        stack = self._stack()
        order = stack.submit_order(stack.proposal(quantity=1.0, price=100.0), now_ms=ts(0))

        stack.cancel(order, now_ms=ts(0))  # 生效时刻 = ts(40)
        stack.feed([sell_aggressor(1, price=100.0, quantity=3.5, offset=20)])

        self.assertAlmostEqual(stack.order(order.client_order_id).filled_quantity, 0.5)
        self.assertIs(stack.order(order.client_order_id).status, OrderStatus.PARTIALLY_FILLED)

    def test_sc11_cancel_ack_reports_the_executed_quantity(self) -> None:
        stack = self._stack()
        order = stack.submit_order(stack.proposal(quantity=1.0, price=100.0), now_ms=ts(0))
        stack.cancel(order, now_ms=ts(0))
        stack.feed([sell_aggressor(1, price=100.0, quantity=3.5, offset=20)])

        stack.feed([book_delta(2, 2, bids=((100.0, 3.0),), offset=CANCEL_LATENCY_MS)])

        self.assertIs(stack.order(order.client_order_id).status, OrderStatus.CANCELED)
        canceled = [event for event in stack.produced if type(event).__name__ == "OrderCanceled"][-1]
        self.assertAlmostEqual(canceled.executed_quantity or 0.0, 0.5)

    def test_sc11_no_fill_after_the_cancel_is_effective(self) -> None:
        stack = self._stack()
        order = stack.submit_order(stack.proposal(quantity=1.0, price=100.0), now_ms=ts(0))
        stack.cancel(order, now_ms=ts(0))

        stack.feed(
            [
                book_delta(2, 2, bids=((100.0, 3.0),), offset=CANCEL_LATENCY_MS),
                sell_aggressor(1, price=100.0, quantity=99.0, offset=CANCEL_LATENCY_MS + 1),
            ]
        )

        self.assertIs(stack.order(order.client_order_id).status, OrderStatus.CANCELED)
        self.assertAlmostEqual(stack.order(order.client_order_id).filled_quantity, 0.0)
        self.assertEqual(stack.venue.fills, ())

    def test_sc11_trade_exactly_at_the_effective_time_loses_to_the_cancel(self) -> None:
        stack = self._stack()
        order = stack.submit_order(stack.proposal(quantity=1.0, price=100.0), now_ms=ts(0))
        stack.cancel(order, now_ms=ts(0))

        stack.feed([sell_aggressor(1, price=100.0, quantity=99.0, offset=CANCEL_LATENCY_MS)])

        self.assertIs(stack.order(order.client_order_id).status, OrderStatus.CANCELED)
        self.assertEqual(stack.venue.fills, ())

    def test_trade_just_before_the_effective_time_still_fills(self) -> None:
        stack = self._stack()
        order = stack.submit_order(stack.proposal(quantity=1.0, price=100.0), now_ms=ts(0))
        stack.cancel(order, now_ms=ts(0))

        stack.feed([sell_aggressor(1, price=100.0, quantity=99.0, offset=CANCEL_LATENCY_MS - 1)])

        self.assertIs(stack.order(order.client_order_id).status, OrderStatus.FILLED)
        self.assertEqual(len(stack.venue.fills), 1)

    def test_cancel_after_full_fill_does_not_resurrect_the_order(self) -> None:
        stack = self._stack()
        order = stack.submit_order(stack.proposal(quantity=0.5, price=100.0), now_ms=ts(0))

        result = stack.cancel(order, now_ms=ts(0))
        stack.feed([sell_aggressor(1, price=100.0, quantity=99.0, offset=1)])
        second = stack.venue.cancel(stack.order(order.client_order_id))

        self.assertEqual(result.updates, ())
        self.assertEqual(second, ())
        self.assertIs(stack.order(order.client_order_id).status, OrderStatus.FILLED)

    def test_repeated_cancel_requests_are_idempotent(self) -> None:
        stack = self._stack()
        order = stack.submit_order(stack.proposal(quantity=1.0, price=100.0), now_ms=ts(0))

        first = stack.cancel(order, now_ms=ts(0))
        second = stack.cancel(order, now_ms=ts(5))
        stack.feed([book_delta(2, 2, bids=((100.0, 3.0),), offset=CANCEL_LATENCY_MS)])

        self.assertEqual(first.updates, ())
        self.assertEqual(second.updates, ())
        self.assertEqual(
            len([event for event in stack.produced if type(event).__name__ == "OrderCanceled"]), 1
        )


if __name__ == "__main__":
    unittest.main()
