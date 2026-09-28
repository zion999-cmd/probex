"""SC-2：L2 盘口正确性。"""

from __future__ import annotations

import unittest

from market.book.order_book import BookSide, DeltaOutcome, OrderBook
from market.events.payloads import BookDeltaPayload, BookSnapshotPayload, PriceLevel
from market.events.types import Venue
from tests.support import SYMBOL


def _level(price: float, size: float) -> PriceLevel:
    return PriceLevel(price=price, size=size)


def _snapshot(last_update_id: int, bids=(), asks=()) -> BookSnapshotPayload:
    return BookSnapshotPayload(last_update_id=last_update_id, bids=tuple(bids), asks=tuple(asks))


def _delta(first: int, last: int, bids=(), asks=()) -> BookDeltaPayload:
    return BookDeltaPayload(first_update_id=first, last_update_id=last, bids=tuple(bids), asks=tuple(asks))


class OrderBookAcceptanceTest(unittest.TestCase):
    """SC-2 验收：快照 + 连续增量后的档位与交易所语义一致。"""

    def test_sc2_levels_match_expected_after_snapshot_and_deltas(self) -> None:
        book = OrderBook(Venue.BINANCE, "BTCUSDT")
        book.apply_snapshot(
            _snapshot(
                100,
                bids=[_level(100.0, 1.0), _level(99.5, 2.0)],
                asks=[_level(100.5, 1.5), _level(101.0, 3.0)],
            )
        )
        # 更新数量
        self.assertIs(book.apply_delta(_delta(101, 101, bids=[_level(100.0, 2.0)])), DeltaOutcome.APPLIED)
        # 删除档位
        self.assertIs(book.apply_delta(_delta(102, 102, asks=[_level(100.5, 0.0)])), DeltaOutcome.APPLIED)
        # 新增档位
        self.assertIs(book.apply_delta(_delta(103, 103, bids=[_level(99.0, 5.0)])), DeltaOutcome.APPLIED)

        self.assertEqual(book.depth(BookSide.BID), (_level(100.0, 2.0), _level(99.5, 2.0), _level(99.0, 5.0)))
        self.assertEqual(book.depth(BookSide.ASK), (_level(101.0, 3.0),))
        self.assertEqual(book.best_bid(), _level(100.0, 2.0))
        self.assertEqual(book.best_ask(), _level(101.0, 3.0))
        self.assertEqual(book.last_update_id, 103)
        self.assertEqual(book.level_count(BookSide.BID), 3)
        self.assertEqual(book.level_count(BookSide.ASK), 1)


class OrderBookTest(unittest.TestCase):
    def setUp(self) -> None:
        self.book = OrderBook(Venue.BINANCE, "BTCUSDT")

    def test_requires_symbol(self) -> None:
        with self.assertRaises(ValueError):
            OrderBook(Venue.BINANCE, "")

    def test_uninitialized_book_has_no_levels(self) -> None:
        self.assertFalse(self.book.is_initialized)
        self.assertIsNone(self.book.last_update_id)
        self.assertIsNone(self.book.best_bid())
        self.assertIsNone(self.book.best_ask())
        self.assertEqual(self.book.depth(BookSide.BID), ())

    def test_delta_before_snapshot_is_awaiting_snapshot(self) -> None:
        self.assertIs(self.book.apply_delta(_delta(1, 1, bids=[_level(100.0, 1.0)])), DeltaOutcome.AWAITING_SNAPSHOT)
        self.assertEqual(self.book.level_count(BookSide.BID), 0)

    def test_snapshot_replaces_previous_levels(self) -> None:
        self.book.apply_snapshot(_snapshot(5, bids=[_level(100.0, 1.0)], asks=[_level(101.0, 1.0)]))
        self.book.apply_snapshot(_snapshot(9, bids=[_level(99.0, 2.0)], asks=[_level(102.0, 2.0)]))
        self.assertEqual(self.book.depth(BookSide.BID), (_level(99.0, 2.0),))
        self.assertEqual(self.book.depth(BookSide.ASK), (_level(102.0, 2.0),))
        self.assertEqual(self.book.last_update_id, 9)

    def test_duplicate_sequence_is_ignored(self) -> None:
        self.book.apply_snapshot(_snapshot(10, bids=[_level(100.0, 1.0)]))
        self.book.apply_delta(_delta(11, 11, bids=[_level(100.0, 2.0)]))
        self.assertIs(self.book.apply_delta(_delta(11, 11, bids=[_level(100.0, 9.0)])), DeltaOutcome.ALREADY_APPLIED)
        self.assertEqual(self.book.best_bid(), _level(100.0, 2.0))
        self.assertEqual(self.book.last_update_id, 11)

    def test_older_sequence_is_ignored(self) -> None:
        self.book.apply_snapshot(_snapshot(10))
        self.book.apply_delta(_delta(11, 12))
        self.assertIs(self.book.apply_delta(_delta(11, 11)), DeltaOutcome.ALREADY_APPLIED)
        self.assertEqual(self.book.last_update_id, 12)

    def test_gap_is_detected_without_mutating_book(self) -> None:
        self.book.apply_snapshot(_snapshot(10, bids=[_level(100.0, 1.0)]))
        self.assertIs(self.book.apply_delta(_delta(12, 12, bids=[_level(100.0, 5.0)])), DeltaOutcome.GAP)
        self.assertEqual(self.book.best_bid(), _level(100.0, 1.0))
        self.assertEqual(self.book.last_update_id, 10)

    def test_overlapping_delta_is_applied(self) -> None:
        self.book.apply_snapshot(_snapshot(10))
        # U <= last + 1 <= u 允许重叠
        self.assertIs(self.book.apply_delta(_delta(10, 12, bids=[_level(100.0, 7.0)])), DeltaOutcome.APPLIED)
        self.assertEqual(self.book.last_update_id, 12)
        self.assertEqual(self.book.best_bid(), _level(100.0, 7.0))

    def test_depth_limit_and_ordering(self) -> None:
        self.book.apply_snapshot(
            _snapshot(
                1,
                bids=[_level(100.0, 1.0), _level(99.0, 1.0), _level(98.0, 1.0)],
                asks=[_level(101.0, 1.0), _level(102.0, 1.0), _level(103.0, 1.0)],
            )
        )
        self.assertEqual([lvl.price for lvl in self.book.depth(BookSide.BID, 2)], [100.0, 99.0])
        self.assertEqual([lvl.price for lvl in self.book.depth(BookSide.ASK, 2)], [101.0, 102.0])

    def test_negative_limit_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.book.depth(BookSide.BID, -1)

    def test_zero_limit_returns_empty(self) -> None:
        self.book.apply_snapshot(_snapshot(1, bids=[_level(100.0, 1.0)]))
        self.assertEqual(self.book.depth(BookSide.BID, 0), ())

    def test_deleting_absent_level_is_noop(self) -> None:
        self.book.apply_snapshot(_snapshot(1, bids=[_level(100.0, 1.0)]))
        self.assertIs(self.book.apply_delta(_delta(2, 2, bids=[_level(97.0, 0.0)])), DeltaOutcome.APPLIED)
        self.assertEqual(self.book.best_bid(), _level(100.0, 1.0))

    def test_reset_clears_book(self) -> None:
        self.book.apply_snapshot(_snapshot(1, bids=[_level(100.0, 1.0)]))
        self.book.reset()
        self.assertFalse(self.book.is_initialized)
        self.assertEqual(self.book.depth(BookSide.BID), ())

    def test_cached_ordering_stays_correct_after_mutation(self) -> None:
        self.book.apply_snapshot(_snapshot(1, bids=[_level(100.0, 1.0)]))
        self.assertEqual(self.book.best_bid(), _level(100.0, 1.0))
        self.book.apply_delta(_delta(2, 2, bids=[_level(101.0, 1.0)]))
        self.assertEqual(self.book.best_bid(), _level(101.0, 1.0))
        self.book.apply_delta(_delta(3, 3, bids=[_level(101.0, 0.0)]))
        self.assertEqual(self.book.best_bid(), _level(100.0, 1.0))


class FuturesContinuityRuleTest(unittest.TestCase):
    """P0001.9.1.1 SC-2/SC-3/SC-4：venue-aware 连续性判据。

    Binance Futures 的 diff 事件会聚合上万个 update id ⇒ `U != last+1` 是常态；
    正确判据是 `pu == last`。**但快照锚点处的第一条增量**按 vendor 文档用窗口规则
    （REST 快照的 `lastUpdateId` 不是「上一条推送」，`pu` 在该刻不可比）。
    没有 `pu` 的增量（现货）始终走窗口规则。
    """

    def _book_with(self, last_update_id: int):
        book = OrderBook(Venue.BINANCE, SYMBOL)
        book.apply_snapshot(
            BookSnapshotPayload(
                last_update_id=last_update_id,
                bids=(PriceLevel(price=100.0, size=1.0),),
                asks=(PriceLevel(price=101.0, size=1.0),),
            )
        )
        return book

    def _delta(self, first: int, last: int, *, pu: int | None):
        return BookDeltaPayload(
            first_update_id=first,
            last_update_id=last,
            bids=(PriceLevel(price=100.0, size=2.0),),
            asks=(),
            previous_update_id=pu,
        )

    def _anchored_book(self, snapshot_last: int) -> tuple[OrderBook, int]:
        """快照 + 锚点增量（满足窗口规则），返回 `(book, 锚点后的 last_update_id)`。"""
        book = self._book_with(snapshot_last)
        anchor_last = snapshot_last + 500
        outcome = book.apply_delta(self._delta(snapshot_last + 1, anchor_last, pu=None))
        self.assertIs(outcome, DeltaOutcome.APPLIED)
        return book, anchor_last

    def test_anchor_delta_must_straddle_the_snapshot_point(self) -> None:
        """Futures 锚点：`pu <= L < u` 即快照序号落在该增量覆盖区间内 ⇒ 应用。"""
        book = self._book_with(1000)

        outcome = book.apply_delta(self._delta(1070, 6000, pu=1000))

        self.assertIs(outcome, DeltaOutcome.APPLIED)
        self.assertEqual(book.last_update_id, 6000)

    def test_anchor_delta_before_the_snapshot_is_already_applied(self) -> None:
        book = self._book_with(1000)

        outcome = book.apply_delta(self._delta(900, 1000, pu=800))

        self.assertIs(outcome, DeltaOutcome.ALREADY_APPLIED)

    def test_anchor_delta_starting_after_the_snapshot_is_a_gap(self) -> None:
        """`pu > L` ⇒ (L, pu] 的推送没被应用过 ⇒ 真实缺口，必须 resync。"""
        book = self._book_with(1000)

        outcome = book.apply_delta(self._delta(5000, 6000, pu=1100))

        self.assertIs(outcome, DeltaOutcome.GAP)
        self.assertEqual(book.last_update_id, 1000)

    def test_anchor_delta_without_pu_uses_the_window_rule(self) -> None:
        book = self._book_with(1000)

        applied = book.apply_delta(self._delta(1001, 1500, pu=None))
        book2 = self._book_with(1000)
        gap = book2.apply_delta(self._delta(5000, 6000, pu=None))

        self.assertIs(applied, DeltaOutcome.APPLIED)
        self.assertIs(gap, DeltaOutcome.GAP)

    def test_sc2_aggregated_jump_with_matching_pu_is_contiguous(self) -> None:
        book, anchor_last = self._anchored_book(11674982854761)

        outcome = book.apply_delta(self._delta(anchor_last + 4, anchor_last + 20000, pu=anchor_last))

        self.assertIs(outcome, DeltaOutcome.APPLIED)
        self.assertEqual(book.last_update_id, anchor_last + 20000)

    def test_sc2_long_run_of_real_shaped_deltas_stays_contiguous(self) -> None:
        """真实形态：连续 50 条带 pu 的聚合增量，逐条 U 跳跃但 pu 严格接续。"""
        book, previous_u = self._anchored_book(1000)

        for step in range(50):
            first = previous_u + 2 + step
            last = previous_u + 200 + step * 7
            outcome = book.apply_delta(self._delta(first, last, pu=previous_u))
            self.assertIs(outcome, DeltaOutcome.APPLIED, f"step {step}")
            previous_u = last

    def test_sc3_mismatched_pu_is_a_real_gap(self) -> None:
        book, anchor_last = self._anchored_book(1000)

        outcome = book.apply_delta(self._delta(anchor_last + 200, anchor_last + 300, pu=anchor_last + 100))

        self.assertIs(outcome, DeltaOutcome.GAP)
        self.assertEqual(book.last_update_id, anchor_last)  # 未被应用

    def test_sc3_gap_after_aggregated_events(self) -> None:
        book, anchor_last = self._anchored_book(1000)
        book.apply_delta(self._delta(anchor_last + 10, anchor_last + 5000, pu=anchor_last))

        outcome = book.apply_delta(self._delta(anchor_last + 6000, anchor_last + 7000, pu=anchor_last + 5500))

        self.assertIs(outcome, DeltaOutcome.GAP)

    def test_sc4_window_rule_still_applies_without_pu(self) -> None:
        """现货语义不变：没有 `pu` 的增量（含锚点之后）一律走窗口规则。"""
        book, anchor_last = self._anchored_book(1000)

        applied = book.apply_delta(self._delta(anchor_last, anchor_last + 5, pu=None))
        gap = book.apply_delta(self._delta(anchor_last + 5000, anchor_last + 6000, pu=None))

        self.assertIs(applied, DeltaOutcome.APPLIED)
        self.assertIs(gap, DeltaOutcome.GAP)

    def test_stale_delta_is_already_applied_even_with_matching_pu(self) -> None:
        book = self._book_with(2000)

        outcome = book.apply_delta(self._delta(1500, 2000, pu=1000))

        self.assertIs(outcome, DeltaOutcome.ALREADY_APPLIED)

    def test_reset_returns_to_the_anchor_state(self) -> None:
        book, anchor_last = self._anchored_book(1000)
        book.apply_delta(self._delta(anchor_last + 10, anchor_last + 20, pu=anchor_last))

        book.reset()

        self.assertIsNone(book.last_update_id)
        self.assertIs(book.apply_delta(self._delta(1, 2, pu=0)), DeltaOutcome.AWAITING_SNAPSHOT)


if __name__ == "__main__":
    unittest.main()
