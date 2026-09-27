"""SC-3 / SC-5：sequence 故障与容错。"""

from __future__ import annotations

import unittest

from market.book.market_book import MarketBook
from market.book.order_book import BookSide, DeltaOutcome
from market.events.types import Venue
from market.health.state import BookHealth
from tests import scenarios
from tests.support import SYMBOL, depth_snapshot_event, feed


def _synced_book() -> MarketBook:
    """已在序号 105 处于 HEALTHY 的盘口。"""
    book = MarketBook(Venue.BINANCE, SYMBOL)
    feed(book, scenarios.events_before_gap())
    return book


def _prices(book: MarketBook, side: BookSide, limit: int) -> list[tuple[float, float]]:
    return [(level.price, level.size) for level in book.depth(side, limit)]


def _last_transition_reason(book: MarketBook) -> str:
    transition = book.last_transition
    assert transition is not None
    return transition.reason


class SequenceFaultAcceptanceTest(unittest.TestCase):
    """SC-3 验收：sequence gap 自动进入 STALE 并关闭可交易门。"""

    def test_sc3_gap_enters_stale_and_closes_gate(self) -> None:
        book = _synced_book()
        self.assertIs(book.health, BookHealth.HEALTHY)

        # 跳过增量 106、107，直接投喂 108
        result = book.on_market_event(scenarios.delta_event(108))

        self.assertIs(result.outcome, DeltaOutcome.GAP)
        self.assertIs(book.health, BookHealth.STALE)
        self.assertFalse(book.is_tradeable)
        self.assertTrue(result.resync_required)
        self.assertFalse(result.applied)
        self.assertEqual(book.last_update_id, scenarios.UPDATE_ID_BEFORE_GAP)
        self.assertEqual(book.view().health, BookHealth.STALE)
        self.assertFalse(book.view().is_tradeable)
        self.assertEqual(_last_transition_reason(book), "sequence_gap")


class SequenceToleranceTest(unittest.TestCase):
    """SC-5 验收：重复、过期、乱序与 gap 期间增量的容错。"""

    def setUp(self) -> None:
        self.book = _synced_book()

    def test_duplicate_delta_is_idempotent(self) -> None:
        expected = self.book.view(depth=100)

        result = self.book.on_market_event(scenarios.delta_event(105))

        self.assertIs(result.outcome, DeltaOutcome.ALREADY_APPLIED)
        self.assertFalse(result.applied)
        self.assertFalse(result.resync_required)
        self.assertIs(self.book.health, BookHealth.HEALTHY)
        self.assertEqual(self.book.view(depth=100), expected)

    def test_older_delta_is_ignored(self) -> None:
        expected = self.book.view(depth=100)

        result = self.book.on_market_event(scenarios.delta_event(101))

        self.assertIs(result.outcome, DeltaOutcome.ALREADY_APPLIED)
        self.assertIs(self.book.health, BookHealth.HEALTHY)
        self.assertEqual(self.book.view(depth=100), expected)

    def test_duplicate_delta_after_gap_is_buffered_not_a_new_gap(self) -> None:
        self.book.on_market_event(scenarios.delta_event(108))

        result = self.book.on_market_event(scenarios.delta_event(108))

        self.assertIsNone(result.outcome)
        self.assertEqual(result.buffered_deltas, 1)
        self.assertIs(self.book.health, BookHealth.STALE)
        self.assertEqual(_last_transition_reason(self.book), "sequence_gap")

    def test_stale_period_deltas_are_buffered_and_do_not_touch_book(self) -> None:
        self.book.on_market_event(scenarios.delta_event(108))
        expected = self.book.view(depth=100)

        results = feed(self.book, scenarios.delta_events(109, 110))

        self.assertEqual([result.buffered_deltas for result in results], [1, 2])
        self.assertTrue(all(not result.applied for result in results))
        self.assertEqual(self.book.view(depth=100), expected)
        self.assertEqual(_prices(self.book, BookSide.BID, 2), [(99.5, 2.0), (99.0, 5.0)])

    def test_recovery_snapshot_skips_buffered_deltas_at_or_below_snapshot(self) -> None:
        self.book.on_market_event(scenarios.delta_event(108))
        feed(self.book, scenarios.delta_events(106, 107))
        self.book.request_resync()

        result = self.book.on_market_event(scenarios.recovery_snapshot())

        self.assertIs(result.health_after, BookHealth.HEALTHY)
        self.assertTrue(self.book.is_tradeable)
        self.assertEqual(self.book.last_update_id, scenarios.RECOVERY_SNAPSHOT_LAST_UPDATE_ID)
        self.assertEqual(result.buffered_deltas, 0)

    def test_buffer_keeps_newest_deltas_when_limit_reached(self) -> None:
        book = MarketBook(Venue.BINANCE, SYMBOL, resync_buffer_limit=1)
        feed(book, scenarios.events_before_gap())
        book.on_market_event(scenarios.delta_event(108))
        feed(book, scenarios.delta_events(109, 110))  # 只保留最新一条（110）

        book.request_resync()
        result = book.on_market_event(
            depth_snapshot_event(
                109,
                bids=[(99.5, 4.0), (99.0, 5.0), (98.0, 1.0)],
                asks=[(102.0, 2.5)],
            )
        )

        self.assertIs(result.health_after, BookHealth.HEALTHY)
        self.assertEqual(book.last_update_id, 110)
        self.assertEqual(_prices(book, BookSide.BID, 2), list(scenarios.reference_bids()))


if __name__ == "__main__":
    unittest.main()
