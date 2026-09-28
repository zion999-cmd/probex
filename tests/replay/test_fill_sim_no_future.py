"""Replay：no-future 证明（SC-16）。

若在某个时点 t 之后改写事件流，t 之前已经产生的成交、订单状态与账户状态必须**完全不变**。
"""

from __future__ import annotations

import unittest

from execution.simulation.types import QueueState
from execution.types import OrderStatus
from market.events.types import MarketEvent
from portfolio.types import Side
from risk.limits import RiskLimits
from tests.sim_support import SimStack, book_delta, book_snapshot, sell_aggressor
from tests.support import SYMBOL

#: 分界点：前 k 条事件是「过去」，其余是「未来」。
SPLIT = 4


def past_events() -> tuple[MarketEvent, ...]:
    return (
        book_snapshot(update_id=1, bids=((100.0, 3.0),), asks=((101.0, 2.0),), offset=0),
        book_delta(2, 2, bids=((100.0, 3.0),), offset=1),
        sell_aggressor(1, price=100.0, quantity=3.0, offset=2),  # 清空队列（不成交）
        sell_aggressor(2, price=100.0, quantity=0.25, offset=3),  # 第一笔成交
    )


def future_events(*, aggressive: bool) -> tuple[MarketEvent, ...]:
    """两种「未来」：温和 vs 极端（大额 trade-through / gap / 重复 id）。"""
    if aggressive:
        return (
            sell_aggressor(3, price=90.0, quantity=999.0, offset=4),
            book_delta(50, 50, bids=((100.0, 999.0),), offset=5),
            sell_aggressor(3, price=100.0, quantity=999.0, offset=6),
        )
    return (
        sell_aggressor(3, price=100.0, quantity=0.75, offset=4),
        book_delta(3, 3, bids=((100.0, 2.0),), offset=5),
    )


def run(events: tuple[MarketEvent, ...], *, upto: int | None = None) -> tuple:
    """回放（可只回放到 `upto`），返回订单 + 成交 + 账户的完整状态。"""
    stack = SimStack.build(limits=RiskLimits(max_position_qty=10.0))
    order_id: str | None = None
    for index, event in enumerate(events):
        stack.feed((event,))
        if index == 0:
            order_id = stack.submit_order(stack.proposal(quantity=1.0, price=100.0), now_ms=event.process_ts).client_order_id
        if upto is not None and index + 1 >= upto:
            break
    assert order_id is not None
    order = stack.order(order_id)
    return (
        order.status,
        order.filled_quantity,
        tuple((fill.quantity, fill.fill_reason.value, fill.event_ordinal) for fill in stack.venue.fills),
        stack.accounting.fills.fills,
        (stack.accounting.balance, stack.accounting.position(SYMBOL).qty),
        stack.venue.order_view(order_id).queue_state,
    )


class FillSimNoFutureTest(unittest.TestCase):
    def test_changing_the_future_does_not_change_the_past(self) -> None:
        baseline = run(past_events() + future_events(aggressive=False), upto=SPLIT)
        mutated = run(past_events() + future_events(aggressive=True), upto=SPLIT)

        self.assertEqual(baseline, mutated)

    def test_past_state_is_stable_while_the_stream_is_extended(self) -> None:
        events = past_events()
        only_past = run(events)
        extended = run(events + future_events(aggressive=False), upto=SPLIT)

        self.assertEqual(only_past, extended)

    def test_the_split_point_is_not_vacuous(self) -> None:
        """分界点之后确实还会发生成交（否则「未来不影响过去」是空洞的）。"""
        before = run(past_events() + future_events(aggressive=False), upto=SPLIT)
        after = run(past_events() + future_events(aggressive=False))

        self.assertEqual(before[1], 0.25)
        self.assertEqual(after[1], 1.0)
        self.assertNotEqual(before, after)

    def test_future_trade_through_only_affects_its_own_time(self) -> None:
        baseline = run(past_events() + future_events(aggressive=False), upto=SPLIT)
        aggressive = run(past_events() + future_events(aggressive=True))

        self.assertEqual(baseline[0].value, "PARTIALLY_FILLED")
        self.assertEqual(aggressive[0].value, "FILLED")
        self.assertEqual(baseline[3], aggressive[3][: len(baseline[3])])  # 过去的成交是未来的前缀

    def test_recorded_fills_are_append_only(self) -> None:
        """流式回放的关键性质：已经记录的成交证据永不因后续事件被改写。"""
        stack = SimStack.build(limits=RiskLimits(max_position_qty=10.0))
        order_id = ""
        for index, event in enumerate(past_events()):
            stack.feed((event,))
            if index == 0:
                order_id = stack.submit_order(
                    stack.proposal(quantity=1.0, price=100.0), now_ms=event.process_ts
                ).client_order_id
        recorded = stack.venue.fills
        ledger = stack.accounting.fills.fills
        status_at_split = stack.order(order_id).status

        stack.feed(future_events(aggressive=True))

        self.assertEqual(stack.venue.fills[: len(recorded)], recorded)
        self.assertEqual(stack.accounting.fills.fills[: len(ledger)], ledger)
        self.assertIs(status_at_split, OrderStatus.PARTIALLY_FILLED)
        self.assertIs(stack.venue.order_view(order_id).queue_state, QueueState.KNOWN)


if __name__ == "__main__":
    unittest.main()
