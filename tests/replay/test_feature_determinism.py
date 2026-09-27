"""SC-1：同一 Event Store 重放两次得到逐字段相同的 MarketState 序列。"""

from __future__ import annotations

import unittest
from pathlib import Path

from market.replay.source import ReplayMode, ReplaySource
from market.state.types import MarketState
from storage.events.reader import JsonlEventReader
from tests import scenarios
from tests.support import TempDirTestCase, feed_engine, feature_engine, write_store


class FeatureDeterminismTest(TempDirTestCase):
    def _store(self, name: str, events: list) -> Path:
        path = self.store_path(name)
        write_store(path, events)
        return path

    def _replay(self, path: Path) -> tuple[MarketState, ...]:
        events = list(ReplaySource(JsonlEventReader(path)).iter_events())
        return feed_engine(feature_engine(), events)

    def _gap_and_resync_events(self) -> list:
        return [
            *scenarios.events_before_gap(),
            *scenarios.delta_events(108, 109, 110),
            scenarios.recovery_snapshot(),
        ]

    def test_sc1_healthy_stream_is_deterministic(self) -> None:
        path = self._store("healthy.jsonl", scenarios.reference_events())

        first = self._replay(path)
        second = self._replay(path)

        self.assertEqual(len(first), len(second))
        self.assertEqual(first, second)

    def test_sc1_gap_and_resync_stream_is_deterministic(self) -> None:
        path = self._store("faulty.jsonl", self._gap_and_resync_events())

        first = self._replay(path)
        second = self._replay(path)

        self.assertEqual(first, second)
        self.assertEqual([state.time.event_ordinal for state in first], list(range(len(first))))

    def test_sc1_state_sequence_covers_multiple_feature_regimes(self) -> None:
        path = self._store("faulty.jsonl", self._gap_and_resync_events())
        states = self._replay(path)

        self.assertIsNotNone(states[-1].price.mid)
        self.assertTrue(any(not state.quality.book_healthy for state in states))
        self.assertGreater(len({state.quality.book_health for state in states}), 1)
        self.assertGreater(len(set(states)), 1)

    def test_sc1_step_and_full_replay_produce_identical_states(self) -> None:
        path = self._store("healthy.jsonl", scenarios.reference_events())

        full_states = self._replay(path)

        source = ReplaySource(JsonlEventReader(path), mode=ReplayMode.STEP)
        engine = feature_engine()
        step_states: list[MarketState] = []
        while True:
            event = source.next_event()
            if event is None:
                break
            state = engine.on_market_event(event)
            step_states.append(state)

        self.assertEqual(list(full_states), step_states)

    def test_sc1_replaying_the_same_store_file_twice_does_not_modify_it(self) -> None:
        path = self._store("healthy.jsonl", scenarios.reference_events())
        before = path.read_bytes()
        self._replay(path)
        self._replay(path)
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
