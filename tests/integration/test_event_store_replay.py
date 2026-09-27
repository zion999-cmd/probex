"""SC-1 / SC-4 / SC-5 / SC-7：Event Store -> Replay -> MarketBook 集成。"""

from __future__ import annotations

import unittest

from market.book.market_book import MarketBook
from market.book.order_book import BookSide, DeltaOutcome
from market.health.state import BookHealth
from market.replay.source import ReplayMode, ReplayModeError, ReplaySource
from storage.events.reader import JsonlEventReader
from tests import scenarios
from tests.support import TempDirTestCase, feed, market_book, replay_into, write_store


def _prices(book: MarketBook, side: BookSide) -> list[tuple[float, float]]:
    return [(level.price, level.size) for level in book.depth(side)]


class EventStoreReplayAcceptanceTest(TempDirTestCase):
    """把 P0001.1 的场景经由 Event Store 重放。"""

    def _replay(self, events: list, *, mode: ReplayMode = ReplayMode.FULL) -> list:
        path = self.store_path()
        write_store(path, events)
        source = ReplaySource(JsonlEventReader(path), mode=mode)
        return list(source.iter_events())

    def test_sc1_store_round_trip_replays_identical_events(self) -> None:
        events = scenarios.reference_events()
        path = self.store_path()
        write_store(path, events)

        replayed = list(ReplaySource(JsonlEventReader(path)).iter_events())

        self.assertEqual(replayed, events)
        self.assertEqual(len(replayed), len(events))

    def test_sc4_replay_gap_enters_stale(self) -> None:
        path = self.store_path()
        write_store(path, [*scenarios.events_before_gap(), scenarios.delta_event(108)])

        book = market_book()
        updates = feed(book, list(ReplaySource(JsonlEventReader(path)).iter_events()))

        last = updates[-1]
        self.assertIs(last.outcome, DeltaOutcome.GAP)
        self.assertIs(last.health_before, BookHealth.HEALTHY)
        self.assertIs(last.health_after, BookHealth.STALE)
        self.assertTrue(last.resync_required)
        self.assertIs(book.health, BookHealth.STALE)
        self.assertFalse(book.is_tradeable)
        self.assertFalse(book.view().is_tradeable)
        self.assertEqual(book.last_update_id, scenarios.UPDATE_ID_BEFORE_GAP)

    def test_sc5_resync_replay_matches_reference(self) -> None:
        reference_path = self.store_path("reference.jsonl")
        fault_path = self.store_path("fault.jsonl")
        write_store(reference_path, scenarios.reference_events())
        write_store(
            fault_path,
            [
                *scenarios.events_before_gap(),
                *scenarios.delta_events(108, 109, 110),
                scenarios.recovery_snapshot(),
            ],
        )

        reference_run = replay_into(
            market_book(), list(ReplaySource(JsonlEventReader(reference_path)).iter_events())
        )
        fault_run = replay_into(market_book(), list(ReplaySource(JsonlEventReader(fault_path)).iter_events()))

        self.assertIs(reference_run.final_view.health, BookHealth.HEALTHY)
        self.assertIs(fault_run.final_view.health, BookHealth.HEALTHY)
        self.assertTrue(fault_run.final_view.is_tradeable)
        self.assertTrue(
            any(transition.to_health is BookHealth.STALE for transition in fault_run.transitions),
            "故障路径必须真的经过 STALE",
        )
        self.assertEqual(fault_run.final_view.last_update_id, reference_run.final_view.last_update_id)
        self.assertEqual(fault_run.final_view.bids, reference_run.final_view.bids)
        self.assertEqual(fault_run.final_view.asks, reference_run.final_view.asks)
        self.assertEqual(
            [(level.price, level.size) for level in fault_run.final_view.bids],
            list(scenarios.reference_bids()),
        )
        self.assertEqual(
            [(level.price, level.size) for level in fault_run.final_view.asks],
            list(scenarios.reference_asks()),
        )

    def test_sc7_step_advances_exactly_one_event(self) -> None:
        path = self.store_path()
        write_store(path, scenarios.reference_events())
        source = ReplaySource(JsonlEventReader(path), mode=ReplayMode.STEP)
        book = market_book()

        snapshot_event = source.next_event()
        assert snapshot_event is not None
        book.on_market_event(snapshot_event)
        self.assertIs(book.health, BookHealth.HEALTHY)
        previous_update_id = book.last_update_id

        steps = 0
        while True:
            event = source.next_event()
            if event is None:
                break
            steps += 1
            update = book.on_market_event(event)
            self.assertTrue(update.applied)
            self.assertEqual(book.last_update_id, (previous_update_id or 0) + 1)
            previous_update_id = book.last_update_id

        self.assertEqual(steps, scenarios.LAST_SCRIPTED_UPDATE_ID - scenarios.FIRST_SCRIPTED_UPDATE_ID + 1)
        self.assertIsNone(source.next_event())
        self.assertEqual(source.clock.now(), scenarios.delta_event(scenarios.LAST_SCRIPTED_UPDATE_ID).exchange_ts)

    def test_step_and_full_replay_yield_the_same_events(self) -> None:
        events = scenarios.reference_events()
        path = self.store_path()
        write_store(path, events)

        full = list(ReplaySource(JsonlEventReader(path), mode=ReplayMode.FULL).iter_events())
        step_source = ReplaySource(JsonlEventReader(path), mode=ReplayMode.STEP)
        step = []
        while True:
            event = step_source.next_event()
            if event is None:
                break
            step.append(event)

        self.assertEqual(full, step)
        self.assertEqual(full, events)

    def test_step_mode_rejects_full_entry_point(self) -> None:
        path = self.store_path()
        write_store(path, scenarios.reference_events())
        source = ReplaySource(JsonlEventReader(path), mode=ReplayMode.STEP)
        with self.assertRaises(ReplayModeError):
            source.iter_events()

    def test_full_mode_rejects_step_entry_point(self) -> None:
        path = self.store_path()
        write_store(path, scenarios.reference_events())
        source = ReplaySource(JsonlEventReader(path), mode=ReplayMode.FULL)
        with self.assertRaises(ReplayModeError):
            source.next_event()

    def test_step_source_reset_restarts_from_the_beginning(self) -> None:
        path = self.store_path()
        write_store(path, scenarios.reference_events())
        source = ReplaySource(JsonlEventReader(path), mode=ReplayMode.STEP)

        first = [source.next_event(), source.next_event()]
        source.reset()
        self.assertEqual(source.clock.now(), 0)
        second = [source.next_event(), source.next_event()]

        self.assertEqual(first, second)
        self.assertNotIn(None, first)
        self.assertEqual(source.clock.now(), scenarios.delta_event(101).exchange_ts)

    def test_replay_drives_a_fresh_book_to_the_reference_state(self) -> None:
        path = self.store_path()
        write_store(path, scenarios.reference_events())
        book = market_book()

        for event in ReplaySource(JsonlEventReader(path)).iter_events():
            book.on_market_event(event)

        self.assertIs(book.health, BookHealth.HEALTHY)
        self.assertTrue(book.is_tradeable)
        self.assertEqual(book.last_update_id, scenarios.LAST_SCRIPTED_UPDATE_ID)
        self.assertEqual(_prices(book, BookSide.BID), list(scenarios.reference_bids()))
        self.assertEqual(_prices(book, BookSide.ASK), list(scenarios.reference_asks()))


if __name__ == "__main__":
    unittest.main()
