"""SC-3 / SC-8：Event Store 重放的确定性与时间无关性。"""

from __future__ import annotations

import time
import unittest
from pathlib import Path
from unittest import mock

from market.book.order_book import BookSide
from market.events.types import MarketEvent
from market.health.state import BookHealth
from market.replay.source import ReplayMode, ReplaySource
from storage.events.reader import JsonlEventReader
from tests import scenarios
from tests.support import (
    BASE_TS,
    BookRun,
    TempDirTestCase,
    depth_diff_event,
    depth_snapshot_event,
    market_book,
    replay_into,
    write_store,
)

HOUR_MS = 3_600_000


def _prices(run: BookRun, side: BookSide) -> list[tuple[float, float]]:
    levels = run.final_view.bids if side is BookSide.BID else run.final_view.asks
    return [(level.price, level.size) for level in levels]


class ReplayDeterminismTest(TempDirTestCase):
    """SC-3 验收：同一 Store 连续 Replay 两次结果完全一致。"""

    def setUp(self) -> None:
        super().setUp()
        self.path = self.store_path()
        write_store(
            self.path,
            [
                *scenarios.events_before_gap(),
                *scenarios.delta_events(108, 109, 110),
                scenarios.recovery_snapshot(),
            ],
        )

    def _replay(self) -> BookRun:
        return replay_into(market_book(), list(ReplaySource(JsonlEventReader(self.path)).iter_events()))

    def test_sc3_two_replays_produce_identical_outcomes(self) -> None:
        first = self._replay()
        second = self._replay()

        self.assertEqual(first.updates, second.updates)
        self.assertEqual(first.transitions, second.transitions)
        self.assertEqual(first.final_view, second.final_view)

    def test_sc3_consumption_order_is_identical(self) -> None:
        def event_ids() -> list[str]:
            return [record.event_id for record in JsonlEventReader(self.path)]

        self.assertEqual(event_ids(), event_ids())

    def test_sc3_replay_actually_crosses_stale_and_recovers(self) -> None:
        run = self._replay()
        transitions = [transition.to_health for transition in run.transitions]

        self.assertIn(BookHealth.STALE, transitions)
        self.assertEqual(transitions[-1], BookHealth.HEALTHY)
        self.assertIs(run.final_view.health, BookHealth.HEALTHY)
        self.assertEqual(run.final_view.last_update_id, scenarios.LAST_SCRIPTED_UPDATE_ID)
        self.assertEqual(_prices(run, BookSide.BID), list(scenarios.reference_bids()))

    def test_sc3_full_and_step_replays_match(self) -> None:
        full_run = self._replay()

        step_source = ReplaySource(JsonlEventReader(self.path), mode=ReplayMode.STEP)
        step_book = market_book()
        events: list[MarketEvent] = []
        while True:
            event = step_source.next_event()
            if event is None:
                break
            events.append(event)
            step_book.on_market_event(event)
            if step_book.health is BookHealth.STALE:
                step_book.request_resync()

        self.assertEqual([update.event_type for update in full_run.updates], [event.event_type for event in events])
        self.assertEqual(full_run.final_view, step_book.view(depth=1000))

    def test_sc3_same_store_file_produces_same_events_after_reset(self) -> None:
        source = ReplaySource(JsonlEventReader(self.path))
        first = list(source.iter_events())
        source.reset()
        second = list(source.iter_events())

        self.assertEqual(first, second)
        # 逻辑时钟只前进不后退：最终停在事件时间最大值
        self.assertEqual(source.clock.now(), max(event.exchange_ts for event in first))
        self.assertEqual(source.clock.now(), scenarios.BASE_TS + scenarios.LAST_SCRIPTED_UPDATE_ID)


class ReplayNoSleepTest(TempDirTestCase):
    """SC-8 验收：Replay 按事件推进逻辑时间，不 sleep，不依赖机器速度。"""

    def setUp(self) -> None:
        super().setUp()
        self.path = self.store_path()
        write_store(
            self.path,
            [
                depth_snapshot_event(
                    100,
                    bids=[(100.0, 1.0)],
                    asks=[(100.5, 1.5)],
                    exchange_ts=BASE_TS,
                    receive_ts=BASE_TS + 1,
                    process_ts=BASE_TS + 2,
                ),
                depth_diff_event(
                    101,
                    101,
                    bids=[(100.0, 2.0)],
                    exchange_ts=BASE_TS + HOUR_MS,
                    receive_ts=BASE_TS + HOUR_MS + 1,
                    process_ts=BASE_TS + HOUR_MS + 2,
                ),
                depth_diff_event(
                    102,
                    102,
                    asks=[(100.5, 0.0)],
                    exchange_ts=BASE_TS + 2 * HOUR_MS,
                    receive_ts=BASE_TS + 2 * HOUR_MS + 1,
                    process_ts=BASE_TS + 2 * HOUR_MS + 2,
                ),
                depth_diff_event(
                    103,
                    103,
                    bids=[(99.0, 1.0)],
                    exchange_ts=BASE_TS + 3 * HOUR_MS,
                    receive_ts=BASE_TS + 3 * HOUR_MS + 1,
                    process_ts=BASE_TS + 3 * HOUR_MS + 2,
                ),
            ],
        )

    def test_sc8_replay_does_not_call_sleep(self) -> None:
        source = ReplaySource(JsonlEventReader(self.path))

        with mock.patch("time.sleep", side_effect=AssertionError("replay must not sleep")):
            events = list(source.iter_events())

        self.assertEqual(len(events), 4)
        self.assertEqual(source.clock.now(), BASE_TS + 3 * HOUR_MS)
        self.assertGreater(source.clock.now() - events[0].exchange_ts, HOUR_MS)

    def test_sc8_replay_elapsed_time_is_independent_of_logical_span(self) -> None:
        source = ReplaySource(JsonlEventReader(self.path))

        started = time.monotonic()
        list(source.iter_events())
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 5.0, "逻辑时间跨越 3 小时的 Replay 不应消耗真实时间")

    def test_sc8_clock_is_advanced_by_events_in_order(self) -> None:
        source = ReplaySource(JsonlEventReader(self.path), mode=ReplayMode.STEP)

        observed: list[int] = []
        while True:
            event = source.next_event()
            if event is None:
                break
            observed.append(source.clock.now())

        self.assertEqual(observed, [BASE_TS, BASE_TS + HOUR_MS, BASE_TS + 2 * HOUR_MS, BASE_TS + 3 * HOUR_MS])

    def test_store_file_is_not_modified_by_replay(self) -> None:
        before = Path(self.path).read_bytes()
        list(ReplaySource(JsonlEventReader(self.path)).iter_events())
        self.assertEqual(Path(self.path).read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
