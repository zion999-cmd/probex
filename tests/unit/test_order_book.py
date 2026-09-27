"""SC-2：L2 盘口正确性。"""

from __future__ import annotations

import unittest

from market.book.order_book import BookSide, DeltaOutcome, OrderBook
from market.events.payloads import BookDeltaPayload, BookSnapshotPayload, PriceLevel
from market.events.types import Venue


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


if __name__ == "__main__":
    unittest.main()
