"""SC-5：event-domain OFI。"""

from __future__ import annotations

import unittest

from market.book.order_book import BookMutation, BookSide
from market.events.payloads import PriceLevel
from market.features.flow import FlowFeaturesCalculator, mutation_ofi
from tests.support import BASE_TS, depth_diff_event, depth_snapshot_event, feature_engine

T = BASE_TS


def _level(price: float, size: float) -> PriceLevel | None:
    return PriceLevel(price=price, size=size)


def _best(price: float, size: float) -> PriceLevel | None:
    return PriceLevel(price=price, size=size)


def _mutation(
    *,
    side: BookSide = BookSide.BID,
    price: float = 100.0,
    old_size: float,
    new_size: float,
    best_bid_before: PriceLevel | None,
    best_bid_after: PriceLevel | None,
    best_ask_before: PriceLevel | None = None,
    best_ask_after: PriceLevel | None = None,
) -> BookMutation:
    return BookMutation(
        side=side,
        price=price,
        old_size=old_size,
        new_size=new_size,
        best_bid_before=best_bid_before,
        best_ask_before=best_ask_before,
        best_bid_after=best_bid_after,
        best_ask_after=best_ask_after,
    )


class MutationOfiFormulaTest(unittest.TestCase):
    """Cont–Kukanov–Stoikov 形式的逐 mutation 贡献。"""

    def test_bid_price_improves_adds_new_size(self) -> None:
        mutation = _mutation(old_size=0.0, new_size=3.0, best_bid_before=_best(100.0, 1.0), best_bid_after=_best(101.0, 3.0))
        self.assertEqual(mutation_ofi(mutation), 3.0)

    def test_bid_price_worsens_removes_old_size(self) -> None:
        mutation = _mutation(old_size=1.0, new_size=0.0, best_bid_before=_best(100.0, 1.0), best_bid_after=_best(99.0, 4.0))
        self.assertEqual(mutation_ofi(mutation), -1.0)

    def test_bid_price_unchanged_uses_size_delta(self) -> None:
        mutation = _mutation(old_size=1.0, new_size=5.0, best_bid_before=_best(100.0, 1.0), best_bid_after=_best(100.0, 5.0))
        self.assertEqual(mutation_ofi(mutation), 4.0)

    def test_bid_side_appears_and_disappears(self) -> None:
        appears = _mutation(old_size=0.0, new_size=5.0, best_bid_before=None, best_bid_after=_best(100.0, 5.0))
        disappears = _mutation(old_size=5.0, new_size=0.0, best_bid_before=_best(100.0, 5.0), best_bid_after=None)
        self.assertEqual(mutation_ofi(appears), 5.0)
        self.assertEqual(mutation_ofi(disappears), -5.0)

    def test_ask_price_improves_subtracts_new_size(self) -> None:
        mutation = _mutation(
            side=BookSide.ASK,
            price=100.0,
            old_size=0.0,
            new_size=3.0,
            best_bid_before=_best(99.0, 1.0),
            best_bid_after=_best(99.0, 1.0),
            best_ask_before=_best(101.0, 2.0),
            best_ask_after=_best(100.0, 3.0),
        )
        self.assertEqual(mutation_ofi(mutation), -3.0)

    def test_ask_price_worsens_adds_old_size(self) -> None:
        mutation = _mutation(
            side=BookSide.ASK,
            old_size=2.0,
            new_size=0.0,
            best_bid_before=None,
            best_bid_after=None,
            best_ask_before=_best(101.0, 2.0),
            best_ask_after=_best(102.0, 4.0),
        )
        self.assertEqual(mutation_ofi(mutation), 2.0)

    def test_ask_price_unchanged_uses_size_delta(self) -> None:
        mutation = _mutation(
            side=BookSide.ASK,
            old_size=2.0,
            new_size=7.0,
            best_bid_before=None,
            best_bid_after=None,
            best_ask_before=_best(101.0, 2.0),
            best_ask_after=_best(101.0, 7.0),
        )
        self.assertEqual(mutation_ofi(mutation), -5.0)

    def test_ask_side_appears_and_disappears(self) -> None:
        appears = _mutation(
            side=BookSide.ASK,
            old_size=0.0,
            new_size=3.0,
            best_bid_before=None,
            best_bid_after=None,
            best_ask_before=None,
            best_ask_after=_best(101.0, 3.0),
        )
        disappears = _mutation(
            side=BookSide.ASK,
            old_size=3.0,
            new_size=0.0,
            best_bid_before=None,
            best_bid_after=None,
            best_ask_before=_best(101.0, 3.0),
            best_ask_after=None,
        )
        self.assertEqual(mutation_ofi(appears), -3.0)
        self.assertEqual(mutation_ofi(disappears), 3.0)

    def test_untouched_top_of_book_contributes_zero(self) -> None:
        mutation = _mutation(
            price=95.0,
            old_size=0.0,
            new_size=10.0,
            best_bid_before=_best(100.0, 1.0),
            best_bid_after=_best(100.0, 1.0),
            best_ask_before=_best(101.0, 1.0),
            best_ask_after=_best(101.0, 1.0),
        )
        self.assertEqual(mutation_ofi(mutation), 0.0)


class EventDomainOfiTest(unittest.TestCase):
    """SC-5 验收：5 → 7 → 5 的档位流。"""

    def _engine_states(self):
        engine = feature_engine()
        states = [
            engine.on_market_event(depth_snapshot_event(100, bids=[(100.0, 5.0)], asks=[(101.0, 2.0)])),
            engine.on_market_event(depth_diff_event(101, 101, bids=[(100.0, 7.0)])),
            engine.on_market_event(depth_diff_event(102, 102, bids=[(100.0, 5.0)])),
        ]
        return states

    def test_sc5_quantity_cycle_is_visible_as_two_events(self) -> None:
        snapshot_state, up_state, down_state = self._engine_states()

        # event-domain：两次变化都被观察到
        self.assertEqual(up_state.flow.event_ofi, 2.0)
        self.assertEqual(down_state.flow.event_ofi, -2.0)
        self.assertEqual(down_state.flow.book_update_count, 2)
        self.assertEqual(down_state.flow.bid_update_count, 2)

        # 窗口内有观测且净额为 0：0.0（有数据）而不是 None（无数据）
        self.assertEqual(down_state.flow.ofi_1s, 0.0)
        self.assertEqual(down_state.flow.normalized_ofi_1s, 0.0)

        # snapshot-only 视角：起止 size 都是 5.0，净变化为 0 —— 这正是 V3 sampled OFI 会漏掉的
        self.assertEqual(snapshot_state.price.bid_size, 5.0)
        self.assertEqual(down_state.price.bid_size, 5.0)
        self.assertEqual(down_state.price.bid_size - snapshot_state.price.bid_size, 0.0)

    def test_unavailable_ofi_window_is_none_not_zero(self) -> None:
        engine = feature_engine()
        state = engine.on_market_event(depth_snapshot_event(100, bids=[(100.0, 5.0)], asks=[(101.0, 2.0)]))
        self.assertIsNone(state.flow.event_ofi)
        self.assertIsNone(state.flow.ofi_1s)

    def test_non_best_level_change_has_zero_ofi_but_counts(self) -> None:
        engine = feature_engine()
        engine.on_market_event(depth_snapshot_event(100, bids=[(100.0, 5.0), (99.0, 1.0)], asks=[(101.0, 2.0)]))
        state = engine.on_market_event(depth_diff_event(101, 101, bids=[(99.0, 4.0)]))

        self.assertEqual(state.flow.event_ofi, 0.0)
        self.assertEqual(state.flow.book_update_count, 1)
        self.assertEqual(state.flow.bid_update_count, 1)

    def test_level_additions_and_removals_are_counted(self) -> None:
        engine = feature_engine()
        engine.on_market_event(depth_snapshot_event(100, bids=[(100.0, 5.0)], asks=[(101.0, 2.0)]))
        added = engine.on_market_event(depth_diff_event(101, 101, bids=[(98.0, 3.0)]))
        removed = engine.on_market_event(depth_diff_event(102, 102, bids=[(98.0, 0.0)]))

        self.assertEqual(added.flow.level_additions, 1)
        self.assertEqual(added.flow.level_removals, 0)
        self.assertEqual(removed.flow.level_additions, 1)
        self.assertEqual(removed.flow.level_removals, 1)
        self.assertEqual(removed.flow.book_update_count, 2)

    def test_normalized_ofi_uses_current_l1_depth(self) -> None:
        engine = feature_engine()
        engine.on_market_event(depth_snapshot_event(100, bids=[(100.0, 5.0)], asks=[(101.0, 2.0)]))
        state = engine.on_market_event(depth_diff_event(101, 101, bids=[(100.0, 8.0)]))

        # event_ofi = +3；L1 深度 = 8 + 2
        self.assertEqual(state.flow.event_ofi, 3.0)
        self.assertAlmostEqual(state.flow.normalized_ofi_1s, 3.0 / 10.0)


class FlowWindowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.calculator = FlowFeaturesCalculator()

    def _mutation(self, old_size: float, new_size: float) -> BookMutation:
        return _mutation(
            old_size=old_size,
            new_size=new_size,
            best_bid_before=_best(100.0, old_size),
            best_bid_after=_best(100.0, new_size),
        )

    def test_rolling_window_aggregates_by_time(self) -> None:
        self.calculator.observe([self._mutation(5.0, 7.0)], timestamp=T)
        self.calculator.observe([self._mutation(7.0, 6.0)], timestamp=T + 2_000)

        at = T + 2_000
        self.assertEqual(self.calculator.snapshot(at=at, best_bid_size=6.0, best_ask_size=1.0).ofi_1s, -1.0)
        self.assertEqual(self.calculator.snapshot(at=at, best_bid_size=6.0, best_ask_size=1.0).ofi_5s, 1.0)
        self.assertEqual(self.calculator.snapshot(at=at, best_bid_size=6.0, best_ask_size=1.0).ofi_15s, 1.0)

    def test_window_expiry_returns_none(self) -> None:
        self.calculator.observe([self._mutation(5.0, 7.0)], timestamp=T)
        snapshot = self.calculator.snapshot(at=T + 1_001, best_bid_size=7.0, best_ask_size=1.0)

        self.assertIsNone(snapshot.ofi_1s)
        self.assertEqual(snapshot.ofi_5s, 2.0)

    def test_normalized_unavailable_without_l1_sizes(self) -> None:
        self.calculator.observe([self._mutation(5.0, 7.0)], timestamp=T)
        snapshot = self.calculator.snapshot(at=T, best_bid_size=None, best_ask_size=None)

        self.assertEqual(snapshot.ofi_1s, 2.0)
        self.assertIsNone(snapshot.normalized_ofi_1s)

    def test_normalized_unavailable_when_depth_is_zero(self) -> None:
        self.calculator.observe([self._mutation(5.0, 7.0)], timestamp=T)
        snapshot = self.calculator.snapshot(at=T, best_bid_size=0.0, best_ask_size=0.0)

        self.assertIsNone(snapshot.normalized_ofi_1s)

    def test_invalidate_windows_clears_rolling_values_but_keeps_counters(self) -> None:
        self.calculator.observe([self._mutation(5.0, 7.0)], timestamp=T)
        self.calculator.invalidate_windows()
        snapshot = self.calculator.snapshot(at=T, best_bid_size=7.0, best_ask_size=1.0)

        self.assertIsNone(snapshot.ofi_1s)
        self.assertIsNone(snapshot.event_ofi)
        self.assertEqual(snapshot.book_update_count, 1)

    def test_no_mutation_returns_none_and_does_not_touch_windows(self) -> None:
        self.assertIsNone(self.calculator.observe([], timestamp=T))
        self.assertIsNone(self.calculator.snapshot(at=T, best_bid_size=1.0, best_ask_size=1.0).ofi_1s)


if __name__ == "__main__":
    unittest.main()
