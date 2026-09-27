"""MarketBook 同步状态机测试。"""

from __future__ import annotations

import unittest

from market.book.errors import UnexpectedMarketEventError
from market.book.market_book import MarketBook
from market.book.order_book import BookSide, DeltaOutcome
from market.events.payloads import PriceLevel
from market.events.types import MarketEvent, Venue
from market.health.state import BookHealth, IllegalHealthTransition
from tests.support import SYMBOL, depth_diff_event, depth_snapshot_event, feed

VENUE = Venue.BINANCE


def _book(*, resync_buffer_limit: int = 1024) -> MarketBook:
    return MarketBook(VENUE, SYMBOL, resync_buffer_limit=resync_buffer_limit)


def _initial_snapshot(last_update_id: int = 100) -> MarketEvent:
    return depth_snapshot_event(
        last_update_id,
        bids=[(100.0, 1.0), (99.5, 2.0)],
        asks=[(100.5, 1.5), (101.0, 3.0)],
    )


def _best_bid(book: MarketBook) -> PriceLevel:
    level = book.best_bid()
    assert level is not None
    return level


def _best_ask(book: MarketBook) -> PriceLevel:
    level = book.best_ask()
    assert level is not None
    return level


def _last_transition_reason(book: MarketBook) -> str:
    transition = book.last_transition
    assert transition is not None
    return transition.reason


class MarketBookSyncTest(unittest.TestCase):
    def test_initial_state_is_awaiting_snapshot_and_not_tradeable(self) -> None:
        book = _book()
        self.assertIs(book.health, BookHealth.AWAITING_SNAPSHOT)
        self.assertFalse(book.is_tradeable)
        self.assertIsNone(book.last_update_id)
        self.assertEqual(book.view().bids, ())
        self.assertIsNone(book.best_bid())
        self.assertIsNone(book.best_ask())
        self.assertIsNone(book.last_transition)

    def test_deltas_before_snapshot_are_buffered_not_applied(self) -> None:
        book = _book()
        results = feed(book, [depth_diff_event(101, 101, bids=[(100.0, 5.0)])])
        self.assertFalse(results[0].applied)
        self.assertEqual(results[0].buffered_deltas, 1)
        self.assertIs(book.health, BookHealth.AWAITING_SNAPSHOT)
        self.assertEqual(book.view().bids, ())

    def test_snapshot_drains_buffer_and_becomes_healthy(self) -> None:
        book = _book()
        feed(
            book,
            [
                depth_diff_event(101, 101, bids=[(100.0, 5.0)]),
                depth_diff_event(102, 102, asks=[(100.5, 0.0)]),
            ],
        )

        result = book.on_market_event(_initial_snapshot(100))

        self.assertIs(result.health_before, BookHealth.AWAITING_SNAPSHOT)
        self.assertIs(result.health_after, BookHealth.HEALTHY)
        self.assertFalse(result.resync_required)
        self.assertTrue(result.applied)
        self.assertEqual(result.buffered_deltas, 0)
        self.assertTrue(book.is_tradeable)
        self.assertEqual(book.last_update_id, 102)
        self.assertEqual(_best_bid(book), PriceLevel(price=100.0, size=5.0))
        self.assertEqual(_best_ask(book), PriceLevel(price=101.0, size=3.0))
        self.assertEqual(_last_transition_reason(book), "resync_complete")

    def test_snapshot_without_buffered_deltas_becomes_healthy(self) -> None:
        book = _book()
        book.on_market_event(_initial_snapshot(100))
        self.assertTrue(book.is_tradeable)
        self.assertEqual(book.last_update_id, 100)

    def test_contiguous_delta_after_snapshot_is_applied(self) -> None:
        book = _book()
        book.on_market_event(_initial_snapshot(100))
        result = book.on_market_event(depth_diff_event(101, 103, bids=[(100.0, 7.0)]))
        self.assertTrue(result.applied)
        self.assertIs(result.outcome, DeltaOutcome.APPLIED)
        self.assertEqual(book.last_update_id, 103)
        self.assertTrue(book.is_tradeable)

    def test_gap_enters_stale_and_closes_trade_gate(self) -> None:
        book = _book()
        book.on_market_event(_initial_snapshot(100))
        result = book.on_market_event(depth_diff_event(105, 105, bids=[(100.0, 9.0)]))

        self.assertIs(result.outcome, DeltaOutcome.GAP)
        self.assertIs(result.health_before, BookHealth.HEALTHY)
        self.assertIs(result.health_after, BookHealth.STALE)
        self.assertTrue(result.resync_required)
        self.assertFalse(result.applied)
        self.assertFalse(book.is_tradeable)
        self.assertEqual(book.last_update_id, 100)
        self.assertEqual(_best_bid(book), PriceLevel(price=100.0, size=1.0))
        self.assertEqual(_last_transition_reason(book), "sequence_gap")

    def test_deltas_arriving_while_stale_are_buffered(self) -> None:
        book = _book()
        book.on_market_event(_initial_snapshot(100))
        book.on_market_event(depth_diff_event(105, 105))
        result = book.on_market_event(depth_diff_event(106, 106, bids=[(100.0, 42.0)]))

        self.assertIsNone(result.outcome)
        self.assertFalse(result.applied)
        self.assertEqual(result.buffered_deltas, 1)
        self.assertEqual(_best_bid(book), PriceLevel(price=100.0, size=1.0))

    def test_request_resync_moves_stale_to_resyncing(self) -> None:
        book = _book()
        book.on_market_event(_initial_snapshot(100))
        book.on_market_event(depth_diff_event(105, 105))
        book.request_resync()

        self.assertIs(book.health, BookHealth.RESYNCING)
        self.assertFalse(book.is_tradeable)
        self.assertEqual(_last_transition_reason(book), "resync_requested")

    def test_request_resync_is_idempotent_while_resyncing(self) -> None:
        book = _book()
        book.on_market_event(_initial_snapshot(100))
        book.on_market_event(depth_diff_event(105, 105))
        book.request_resync()
        book.request_resync()
        self.assertIs(book.health, BookHealth.RESYNCING)

    def test_request_resync_rejected_when_healthy(self) -> None:
        book = _book()
        book.on_market_event(_initial_snapshot(100))
        with self.assertRaises(IllegalHealthTransition):
            book.request_resync()

    def test_request_resync_rejected_before_first_snapshot(self) -> None:
        with self.assertRaises(IllegalHealthTransition):
            _book().request_resync()

    def test_replay_gap_keeps_book_stale(self) -> None:
        book = _book()
        book.on_market_event(_initial_snapshot(100))
        book.on_market_event(depth_diff_event(101, 105))
        book.on_market_event(depth_diff_event(107, 107))  # gap -> STALE，缓冲清空
        feed(book, [depth_diff_event(108, 108), depth_diff_event(109, 109)])
        book.request_resync()

        # 快照过旧（105），缓冲增量起点 108 接不上 -> 仍然 STALE
        result = book.on_market_event(_initial_snapshot(105))

        self.assertIs(result.health_after, BookHealth.STALE)
        self.assertTrue(result.resync_required)
        self.assertEqual(_last_transition_reason(book), "replay_sequence_gap")
        self.assertFalse(book.is_tradeable)

    def test_snapshot_while_healthy_rebases_and_stays_healthy(self) -> None:
        book = _book()
        book.on_market_event(_initial_snapshot(100))
        result = book.on_market_event(_initial_snapshot(110))
        self.assertIs(result.health_before, BookHealth.HEALTHY)
        self.assertIs(result.health_after, BookHealth.HEALTHY)
        self.assertEqual(book.last_update_id, 110)

    def test_buffer_is_bounded(self) -> None:
        book = _book(resync_buffer_limit=2)
        results = feed(
            book,
            [
                depth_diff_event(1, 1, bids=[(100.0, 1.0)]),
                depth_diff_event(2, 2, bids=[(101.0, 1.0)]),
                depth_diff_event(3, 3, bids=[(102.0, 1.0)]),
            ],
        )
        self.assertEqual(results[-1].buffered_deltas, 2)

    def test_buffer_limit_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            MarketBook(VENUE, SYMBOL, resync_buffer_limit=0)

    def test_foreign_symbol_is_rejected(self) -> None:
        book = _book()
        with self.assertRaises(UnexpectedMarketEventError):
            book.on_market_event(depth_diff_event(101, 101, symbol="ETHUSDT"))

    def test_view_reports_health_and_tradeable_flag(self) -> None:
        book = _book()
        book.on_market_event(_initial_snapshot(100))
        view = book.view(depth=1)

        self.assertIs(view.health, BookHealth.HEALTHY)
        self.assertTrue(view.is_tradeable)
        self.assertEqual(view.last_update_id, 100)
        self.assertIs(view.venue, VENUE)
        self.assertEqual(view.symbol, SYMBOL)
        self.assertEqual([(lvl.price, lvl.size) for lvl in view.bids], [(100.0, 1.0)])
        self.assertEqual([(lvl.price, lvl.size) for lvl in view.asks], [(100.5, 1.5)])
        self.assertEqual(book.depth(BookSide.BID, 1), view.bids)

    def test_view_remains_populated_while_stale(self) -> None:
        book = _book()
        book.on_market_event(_initial_snapshot(100))
        book.on_market_event(depth_diff_event(105, 105))
        view = book.view()

        self.assertIs(view.health, BookHealth.STALE)
        self.assertFalse(view.is_tradeable)
        self.assertEqual(len(view.bids), 2)


if __name__ == "__main__":
    unittest.main()
