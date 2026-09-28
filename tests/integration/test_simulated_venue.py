"""P0001.8 集成测试：SimulatedVenue 的 ExecutionAdapter 契约与基本成交推断。

覆盖 SC-1（队列 = 进入时可见数量）、SC-3（正确方向 aggressor 推进队列）、
SC-4（partial / full fill）、SC-6（trade-through）、SC-10（submit latency）、
SC-9（不可观察价位 → QUEUE_UNKNOWN）与 adapter 契约（ack / reject / poll / open_orders）。
"""

from __future__ import annotations

import unittest

from execution.events import FillReceived, OrderAccepted, OrderCanceled, OrderRejected
from execution.types import Order, OrderStatus
from market.events.payloads import AggressorSide
from market.events.types import Venue
from portfolio.types import Side
from risk.limits import RiskLimits
from tests.sim_support import (
    DEFAULT_TEST_FEE_RATE,
    SimStack,
    book_delta,
    book_snapshot,
    buy_aggressor,
    sell_aggressor,
    trade,
    ts,
)
from tests.support import BASE_TS, SYMBOL, make_fill


def _venue_order(client_order_id: str, *, now_ms: int, side: Side = Side.BUY, price: float = 100.0) -> Order:
    from execution.types import Order as _Order

    return _Order(
        client_order_id=client_order_id,
        venue=Venue.BINANCE,
        symbol=SYMBOL,
        side=side,
        price=price,
        quantity=1.0,
        status=OrderStatus.PENDING_CREATE,
        created_at=now_ms,
        updated_at=now_ms,
        post_only=True,
    )


class SimulatedVenueContractTest(unittest.TestCase):
    def test_implements_the_execution_adapter_contract(self) -> None:
        from execution.adapters.base import ExecutionAdapter

        venue = SimStack.build().venue

        self.assertIsInstance(venue, ExecutionAdapter)
        for method in ("submit", "cancel", "poll", "open_orders", "recent_fills"):
            self.assertTrue(callable(getattr(venue, method)))

    def test_submit_returns_ack_with_exchange_id(self) -> None:
        from execution.simulation import SimulatedVenue  # noqa: F401  (显式说明被测对象)

        stack = SimStack.build(submit_latency_ms=30)
        order = _venue_order("probex-s1-000900", now_ms=ts(0))

        events = stack.venue.submit(order)

        self.assertIsInstance(events[0], OrderAccepted)
        self.assertEqual(events[0].exchange_order_id, "sim-000001")
        self.assertEqual(stack.venue.order_view(order.client_order_id).entered, False)

    def test_duplicate_client_order_id_is_rejected(self) -> None:
        stack = SimStack.build()
        order = stack.submit_order(stack.proposal(), now_ms=ts(0))

        rejected = stack.venue.submit(order)

        self.assertIsInstance(rejected[0], OrderRejected)
        self.assertEqual(rejected[0].reason, "DUPLICATE_CLIENT_ORDER_ID")

    def test_post_only_crossing_is_rejected(self) -> None:
        stack = SimStack.build()
        stack.feed([book_snapshot(offset=0)])
        crossing = stack.proposal(side=Side.BUY, price=101.0)

        result = stack.submit(crossing, now_ms=ts(1))

        self.assertTrue(result.submitted)  # 风险门放行；venue rule 在 adapter 层拒绝
        self.assertIs(stack.order(result.order.client_order_id).status, OrderStatus.FAILED)
        self.assertEqual(stack.venue.open_orders(), ())

    def test_cancel_of_unknown_order_is_rejected(self) -> None:
        stack = SimStack.build()
        order = stack.submit_order(stack.proposal(), now_ms=ts(0))
        stack.venue._orders.clear()  # 模拟「外部不存在该订单」

        events = stack.venue.cancel(order)

        self.assertIsInstance(events[0], OrderRejected)
        self.assertEqual(events[0].reason, "CANCEL_UNKNOWN_ORDER")

    def test_cancel_at_the_current_simulation_time_confirms_immediately(self) -> None:
        stack = SimStack.build()
        stack.feed([book_snapshot(offset=0)])
        order = stack.submit_order(stack.proposal(), now_ms=ts(0))

        result = stack.cancel(order, now_ms=ts(0))  # 与模拟时钟同刻 → 立即生效

        self.assertEqual(len(result.updates), 1)
        self.assertIs(stack.order(order.client_order_id).status, OrderStatus.CANCELED)
        self.assertEqual(stack.venue.open_orders(), ())

    def test_cancel_requested_ahead_of_the_simulation_clock_is_deferred(self) -> None:
        """模拟时钟只由市场事件推进：请求时刻尚未到达时不得提前确认（避免未来信息）。"""
        stack = SimStack.build()
        stack.feed([book_snapshot(offset=0)])
        order = stack.submit_order(stack.proposal(), now_ms=ts(0))

        result = stack.cancel(order, now_ms=ts(10))

        self.assertEqual(result.updates, ())
        self.assertIs(stack.order(order.client_order_id).status, OrderStatus.PENDING_CANCEL)
        stack.feed([book_delta(2, 2, bids=((100.0, 3.0),), offset=10)])
        self.assertIs(stack.order(order.client_order_id).status, OrderStatus.CANCELED)

    def test_poll_delivers_each_fill_exactly_once(self) -> None:
        stack = SimStack.build()
        stack.feed([book_snapshot(offset=0)])
        order = stack.submit_order(stack.proposal(quantity=0.5, price=100.0), now_ms=ts(1))

        produced = stack.feed([sell_aggressor(1, price=100.0, quantity=10.0, offset=2)])

        self.assertEqual([type(event) for event in produced], [FillReceived])
        self.assertEqual(stack.venue.poll(), ())
        self.assertEqual(
            [update.outcome.value for update in stack.engine.poll(now_ms=ts(3)).updates], []
        )
        self.assertIs(stack.order(order.client_order_id).status, OrderStatus.FILLED)

    def test_recent_fills_and_open_orders_are_reported(self) -> None:
        stack = SimStack.build()
        stack.feed([book_snapshot(offset=0)])
        order = stack.submit_order(stack.proposal(quantity=0.5, price=100.0), now_ms=ts(1))

        stack.feed([sell_aggressor(1, price=100.0, quantity=10.0, offset=2)])

        self.assertEqual(stack.venue.open_orders(), ())
        fills = stack.venue.recent_fills()
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].client_order_id, order.client_order_id)
        self.assertEqual(fills[0].fee_asset, "USDT")
        self.assertEqual(stack.venue.recent_fills(since_ms=ts(3)), ())


class SimulatedVenueFillTest(unittest.TestCase):
    def _rest(self, *, stack: SimStack, quantity: float = 1.0, price: float = 100.0, side: Side = Side.BUY):
        """在快照时刻挂单（与真实回放循环一致：now_ms 就是刚处理完的事件时间）。"""
        stack.feed([book_snapshot(offset=0)])
        return stack.submit_order(stack.proposal(side=side, quantity=quantity, price=price), now_ms=ts(0))

    def test_sc1_queue_equals_the_visible_size_at_entry(self) -> None:
        stack = SimStack.build()
        order = self._rest(stack=stack)

        view = stack.venue.order_view(order.client_order_id)

        self.assertTrue(view.entered)
        self.assertEqual(view.queue_state.value, "known")
        self.assertEqual(view.queue_ahead, 3.0)
        self.assertEqual(view.initial_queue_ahead, 3.0)
        self.assertEqual(view.queue_rebuild_count, 0)

    def test_sc3_queue_is_consumed_by_aggressor_trades_only(self) -> None:
        stack = SimStack.build()
        order = self._rest(stack=stack)

        stack.feed([sell_aggressor(1, price=100.0, quantity=1.0, offset=2)])
        after_first = stack.venue.order_view(order.client_order_id)
        stack.feed([buy_aggressor(2, price=100.0, quantity=5.0, offset=3)])  # 同方向：不消耗
        after_wrong_side = stack.venue.order_view(order.client_order_id)

        self.assertEqual(after_first.queue_ahead, 2.0)
        self.assertEqual(after_wrong_side.queue_ahead, 2.0)
        self.assertEqual(stack.venue.fills, ())

    def test_sc4_partial_then_full_fill(self) -> None:
        stack = SimStack.build()
        order = self._rest(stack=stack)

        stack.feed(
            [
                sell_aggressor(1, price=100.0, quantity=1.0, offset=2),
                sell_aggressor(2, price=100.0, quantity=2.0, offset=3),
                sell_aggressor(3, price=100.0, quantity=0.4, offset=4),
            ]
        )
        partial = stack.order(order.client_order_id)
        stack.feed([sell_aggressor(4, price=100.0, quantity=0.6, offset=5)])

        self.assertIs(partial.status, OrderStatus.PARTIALLY_FILLED)
        self.assertAlmostEqual(partial.filled_quantity, 0.4)
        self.assertIs(stack.order(order.client_order_id).status, OrderStatus.FILLED)
        self.assertEqual([round(fill.quantity, 6) for fill in stack.venue.fills], [0.4, 0.6])

    def test_sc12_fills_are_maker_and_use_the_order_limit_price(self) -> None:
        stack = SimStack.build(maker_fee_rate=DEFAULT_TEST_FEE_RATE)
        order = self._rest(stack=stack, quantity=0.5)

        stack.feed([sell_aggressor(1, price=100.0, quantity=10.0, offset=2)])

        fill = stack.venue.fills[0]
        self.assertEqual(fill.liquidity.value, "maker")
        self.assertEqual(fill.price, 100.0)
        self.assertEqual(fill.fill_reason.value, "queue_consumed")
        self.assertAlmostEqual(fill.fee, 100.0 * 0.5 * DEFAULT_TEST_FEE_RATE)
        self.assertTrue(stack.produced[0].is_maker)
        self.assertEqual(stack.order(order.client_order_id).avg_fill_price, 100.0)

    def test_sc6_trade_through_is_recorded_separately(self) -> None:
        stack = SimStack.build()
        order = self._rest(stack=stack, quantity=0.5)

        stack.feed([sell_aggressor(1, price=99.5, quantity=0.5, offset=2)])

        fill = stack.venue.fills[0]
        self.assertEqual(fill.fill_reason.value, "trade_through")
        self.assertEqual(fill.price, 100.0)  # 成交价仍是自己的限价
        self.assertIs(stack.order(order.client_order_id).status, OrderStatus.FILLED)

    def test_trade_through_respects_the_observed_volume(self) -> None:
        stack = SimStack.build()
        order = self._rest(stack=stack, quantity=1.0)

        stack.feed([sell_aggressor(1, price=99.0, quantity=0.3, offset=2)])

        self.assertAlmostEqual(stack.order(order.client_order_id).filled_quantity, 0.3)
        self.assertIs(stack.order(order.client_order_id).status, OrderStatus.PARTIALLY_FILLED)

    def test_trade_that_does_not_reach_our_price_is_ignored(self) -> None:
        stack = SimStack.build()
        order = self._rest(stack=stack, quantity=1.0, price=99.0)  # 低于可见最优买价

        stack.feed([sell_aggressor(1, price=100.0, quantity=5.0, offset=2)])

        self.assertEqual(stack.venue.fills, ())
        self.assertAlmostEqual(stack.order(order.client_order_id).filled_quantity, 0.0)

    def test_sell_side_is_filled_by_buy_aggressor(self) -> None:
        stack = SimStack.build()
        order = self._rest(stack=stack, side=Side.SELL, price=101.0, quantity=0.5)

        stack.feed([buy_aggressor(1, price=101.0, quantity=10.0, offset=2)])

        self.assertEqual(stack.venue.fills[0].side, Side.SELL)
        self.assertIs(stack.order(order.client_order_id).status, OrderStatus.FILLED)

    def test_partial_fill_keeps_our_place_at_the_front(self) -> None:
        stack = SimStack.build()
        order = self._rest(stack=stack, quantity=1.0)

        stack.feed(
            [
                sell_aggressor(1, price=100.0, quantity=3.0, offset=2),  # 清空队列
                sell_aggressor(2, price=100.0, quantity=0.25, offset=3),
                sell_aggressor(3, price=100.0, quantity=0.25, offset=4),
            ]
        )

        view = stack.venue.order_view(order.client_order_id)
        self.assertAlmostEqual(view.filled_quantity, 0.5)
        self.assertEqual(view.queue_ahead, 0.0)
        self.assertEqual([round(fill.quantity, 6) for fill in view.fills], [0.25, 0.25])

    def test_sc9_order_price_outside_the_recorded_depth_never_fills(self) -> None:
        stack = SimStack.build()
        order = self._rest(stack=stack, price=99.0, quantity=1.0)

        stack.feed([sell_aggressor(1, price=99.0, quantity=5.0, offset=2)])

        view = stack.venue.order_view(order.client_order_id)
        self.assertEqual(view.queue_state.value, "unknown")
        self.assertIsNone(view.queue_ahead)
        self.assertEqual(stack.venue.fills, ())
        self.assertAlmostEqual(stack.order(order.client_order_id).filled_quantity, 0.0)

    def test_sc10_events_before_entry_do_not_affect_the_order(self) -> None:
        stack = SimStack.build(submit_latency_ms=50)
        stack.feed([book_snapshot(offset=0)])
        order = stack.submit_order(stack.proposal(quantity=1.0, price=100.0), now_ms=ts(0))

        stack.feed([sell_aggressor(1, price=100.0, quantity=99.0, offset=10)])
        before = stack.venue.order_view(order.client_order_id)

        stack.feed([book_delta(2, 2, bids=((100.0, 3.0),), offset=60)])
        after = stack.venue.order_view(order.client_order_id)

        self.assertFalse(before.entered)
        self.assertEqual(before.queue_state.value, "unknown")
        self.assertEqual(stack.venue.fills, ())
        self.assertTrue(after.entered)
        self.assertEqual(after.queue_ahead, 3.0)

    def test_sc10_trade_exactly_at_entry_time_participates(self) -> None:
        stack = SimStack.build(submit_latency_ms=50)
        stack.feed([book_snapshot(offset=0)])
        order = stack.submit_order(stack.proposal(quantity=0.5, price=100.0), now_ms=ts(0))

        stack.feed([sell_aggressor(1, price=100.0, quantity=10.0, offset=50)])

        self.assertIs(stack.order(order.client_order_id).status, OrderStatus.FILLED)

    def test_fills_are_attributed_to_the_recorded_event_ordinal(self) -> None:
        stack = SimStack.build()
        self._rest(stack=stack)

        stack.feed(
            [
                sell_aggressor(1, price=100.0, quantity=1.0, offset=2),
                sell_aggressor(2, price=99.9, quantity=0.5, offset=3),
            ]
        )

        self.assertEqual([fill.event_ordinal for fill in stack.venue.fills], [2])
        self.assertEqual([fill.fill_reason.value for fill in stack.venue.fills], ["trade_through"])
        self.assertEqual(stack.venue.duplicate_trade_count, 0)


class SimulatedVenueRejectTest(unittest.TestCase):
    def test_rejected_submit_does_not_create_a_venue_order(self) -> None:
        stack = SimStack.build()
        stack.feed([book_snapshot(offset=0)])

        stack.submit(stack.proposal(side=Side.SELL, price=100.0), now_ms=ts(1))  # 穿过 best_bid

        self.assertEqual(stack.venue.open_orders(), ())
        self.assertEqual(stack.venue.order_views(), ())

    def test_limits_are_still_enforced_by_the_engine(self) -> None:
        stack = SimStack.build(limits=RiskLimits(max_position_qty=1.0, max_open_order_exposure=100.0))
        stack.feed([book_snapshot(offset=0)])

        first = stack.submit(stack.proposal(quantity=1.0, price=100.0), now_ms=ts(1))
        second = stack.submit(stack.proposal(quantity=1.0, price=100.0), now_ms=ts(2))

        self.assertTrue(first.submitted)
        self.assertTrue(second.rejected)
        self.assertEqual(second.rejection.reason_code.value, "OPEN_ORDER_EXPOSURE_LIMIT")

    def test_position_fill_then_next_quote_sees_the_position(self) -> None:
        stack = SimStack.build()
        stack.feed([book_snapshot(offset=0)])
        first = stack.submit_order(stack.proposal(quantity=0.5, price=100.0), now_ms=ts(1))
        stack.feed([sell_aggressor(1, price=100.0, quantity=10.0, offset=2)])

        position = stack.accounting.position(SYMBOL)

        self.assertAlmostEqual(position.qty, 0.5)
        self.assertAlmostEqual(position.avg_entry_price, 100.0)
        self.assertIs(stack.order(first.client_order_id).status, OrderStatus.FILLED)
        self.assertEqual(stack.accounting.settlement_asset, "USDT")

    def test_seeded_fill_is_the_accounting_baseline_for_comparisons(self) -> None:
        stack = SimStack.build()
        stack.accounting.record_fill(make_fill("seed", Side.BUY, 100.0, 1.0, exchange_ts=ts(0)))

        self.assertAlmostEqual(stack.accounting.position(SYMBOL).qty, 1.0)


if __name__ == "__main__":
    unittest.main()
