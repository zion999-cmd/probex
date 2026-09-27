"""SC-6：同一事件序列重复重放的确定性。"""

from __future__ import annotations

import unittest

from market.book.market_book import BookUpdate, BookView, MarketBook
from market.events.types import MarketEvent, Venue
from market.health.state import BookHealth
from tests import scenarios
from tests.support import SYMBOL, feed


def _replay(events: list[MarketEvent]) -> tuple[list[BookUpdate], BookView]:
    book = MarketBook(Venue.BINANCE, SYMBOL)
    updates = feed(book, events)
    return updates, book.view(depth=1000)


class DeterminismTest(unittest.TestCase):
    """SC-6 验收：两次重放得到完全相同的盘口与健康状态。"""

    def test_sc6_healthy_stream_replays_identically(self) -> None:
        events = scenarios.reference_events()

        first_updates, first_view = _replay(list(events))
        second_updates, second_view = _replay(list(events))

        self.assertEqual(first_updates, second_updates)
        self.assertEqual(first_view, second_view)

    def test_sc6_gap_and_recovery_replays_identically(self) -> None:
        def events() -> list[MarketEvent]:
            return [
                *scenarios.events_before_gap(),
                scenarios.delta_event(108),
                *scenarios.delta_events(109, 110),
                scenarios.recovery_snapshot(),
            ]

        first_updates, first_view = _replay(events())
        second_updates, second_view = _replay(events())

        self.assertEqual(first_updates, second_updates)
        self.assertEqual(first_view, second_view)
        self.assertEqual(first_view.last_update_id, scenarios.LAST_SCRIPTED_UPDATE_ID)

    def test_sc6_view_contains_all_levels_and_health(self) -> None:
        _, view = _replay(scenarios.reference_events())

        self.assertIs(view.health, BookHealth.HEALTHY)
        self.assertEqual(view.last_update_id, scenarios.LAST_SCRIPTED_UPDATE_ID)
        self.assertEqual(
            [(level.price, level.size) for level in view.bids],
            list(scenarios.reference_bids()),
        )
        self.assertEqual(
            [(level.price, level.size) for level in view.asks],
            list(scenarios.reference_asks()),
        )


if __name__ == "__main__":
    unittest.main()
