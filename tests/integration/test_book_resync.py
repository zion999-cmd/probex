"""SC-4：gap 后的恢复闭环。

完整链路：Binance 报文 -> 归一化 -> MarketBook -> 盘口 + BookHealth。
"""

from __future__ import annotations

import unittest

from market.book.market_book import MarketBook
from market.book.order_book import BookSide
from market.events.types import Venue
from market.health.state import BookHealth
from tests import scenarios
from tests.support import SYMBOL, depth_diff_event, feed


def _book() -> MarketBook:
    return MarketBook(Venue.BINANCE, SYMBOL)


def _levels(book: MarketBook, side: BookSide) -> list[tuple[float, float]]:
    return [(level.price, level.size) for level in book.depth(side)]


class BookResyncAcceptanceTest(unittest.TestCase):
    """SC-4 验收：gap 后 resync 回到 HEALTHY，且最终盘口与参考路径一致。"""

    def test_sc4_resync_recovers_healthy(self) -> None:
        # 参考路径：无故障
        reference = _book()
        feed(reference, scenarios.reference_events())
        self.assertIs(reference.health, BookHealth.HEALTHY)

        # 故障路径：快照 100 + 101..105，跳过 106/107，收到 108 触发 gap
        book = _book()
        feed(book, scenarios.events_before_gap())
        gap_result = book.on_market_event(scenarios.delta_event(108))
        self.assertTrue(gap_result.resync_required)
        self.assertIs(book.health, BookHealth.STALE)

        # gap 期间到达的增量被缓冲
        buffered = feed(book, scenarios.delta_events(109, 110))
        self.assertEqual(buffered[-1].buffered_deltas, 2)

        # 重新订阅
        book.request_resync()
        self.assertIs(book.health, BookHealth.RESYNCING)
        self.assertFalse(book.is_tradeable)

        # 新快照 + 重放缓冲增量
        recovery_result = book.on_market_event(scenarios.recovery_snapshot())

        self.assertIs(recovery_result.health_before, BookHealth.RESYNCING)
        self.assertIs(recovery_result.health_after, BookHealth.HEALTHY)
        self.assertFalse(recovery_result.resync_required)
        self.assertEqual(recovery_result.buffered_deltas, 0)
        self.assertIs(book.health, BookHealth.HEALTHY)
        self.assertTrue(book.is_tradeable)

        # 最终盘口与参考路径逐档一致
        self.assertEqual(book.last_update_id, reference.last_update_id)
        self.assertEqual(_levels(book, BookSide.BID), _levels(reference, BookSide.BID))
        self.assertEqual(_levels(book, BookSide.ASK), _levels(reference, BookSide.ASK))
        self.assertEqual(
            _levels(book, BookSide.BID),
            list(scenarios.reference_bids()),
        )
        self.assertEqual(
            _levels(book, BookSide.ASK),
            list(scenarios.reference_asks()),
        )

    def test_healthy_book_reports_gap_free_stream_as_applied(self) -> None:
        book = _book()
        results = feed(book, scenarios.reference_events())

        self.assertTrue(all(result.applied for result in results))
        self.assertTrue(all(not result.resync_required for result in results))
        self.assertTrue(all(result.health_after is BookHealth.HEALTHY for result in results[1:]))
        self.assertIs(results[0].health_after, BookHealth.HEALTHY)

    def test_updates_continue_normally_after_recovery(self) -> None:
        book = _book()
        feed(book, scenarios.events_before_gap())
        book.on_market_event(scenarios.delta_event(108))
        feed(book, scenarios.delta_events(109, 110))
        book.request_resync()
        book.on_market_event(scenarios.recovery_snapshot())

        # 恢复后继续推进
        result = book.on_market_event(depth_diff_event(111, 111, bids=[(97.0, 3.0)]))

        self.assertTrue(result.applied)
        self.assertIs(book.health, BookHealth.HEALTHY)
        self.assertEqual(book.last_update_id, 111)
        self.assertEqual(_levels(book, BookSide.BID)[-1], (97.0, 3.0))

    def test_first_snapshot_is_required_before_any_tradeable_state(self) -> None:
        book = _book()
        results = feed(book, scenarios.delta_events(101, 102))

        self.assertTrue(all(not result.applied for result in results))
        self.assertIs(book.health, BookHealth.AWAITING_SNAPSHOT)
        self.assertFalse(book.is_tradeable)
        self.assertEqual(book.view(depth=100).bids, ())


if __name__ == "__main__":
    unittest.main()
