"""P0001.3 端到端：Event Store → Replay → FeatureEngine → MarketState。"""

from __future__ import annotations

import unittest

from market.events.types import MarketEvent
from market.health.state import BookHealth
from market.replay.source import ReplaySource
from storage.events.reader import JsonlEventReader
from tests import scenarios
from tests.support import (
    BASE_TS,
    TempDirTestCase,
    depth_diff_event,
    depth_snapshot_event,
    feed_engine,
    feature_engine,
    write_store,
)


class FeatureEngineEndToEndTest(TempDirTestCase):
    def test_reference_replay_ends_with_expected_book_features(self) -> None:
        path = self.store_path()
        write_store(path, scenarios.reference_events())

        states = feed_engine(feature_engine(), ReplaySource(JsonlEventReader(path)).iter_events())
        state = states[-1]

        self.assertIs(state.quality.book_health, BookHealth.HEALTHY)
        self.assertEqual(state.price.best_bid, 99.5)
        self.assertEqual(state.price.bid_size, 4.0)
        self.assertEqual(state.price.best_ask, 102.0)
        self.assertEqual(state.price.ask_size, 2.5)
        self.assertEqual(state.price.mid, (99.5 + 102.0) / 2)
        self.assertAlmostEqual(state.price.spread or 0.0, 2.5)
        self.assertEqual(state.depth.bid_depth_1, 4.0)
        self.assertEqual(state.depth.bid_depth_5, 5.0)  # 99.5x4 + 98.0x1
        self.assertEqual(state.depth.ask_depth_5, 2.5)
        # 8 (price) + 14 (depth) + 12 (flow，含已 warm 的窗口) + 0 (returns) + 0 (volatility) = 34/45
        self.assertEqual(state.quality.completeness, 34 / 45)

    def test_event_ordinals_and_times_follow_the_replayed_stream(self) -> None:
        path = self.store_path()
        events = scenarios.reference_events()
        write_store(path, events)

        states = feed_engine(feature_engine(), ReplaySource(JsonlEventReader(path)).iter_events())

        self.assertEqual([state.time.event_ordinal for state in states], list(range(len(events))))
        self.assertEqual(
            [state.time.as_of_exchange_ts for state in states],
            [event.exchange_ts for event in events],
        )

    def test_features_warm_up_over_a_long_healthy_stream(self) -> None:
        engine = feature_engine()
        events: list[MarketEvent] = [
            depth_snapshot_event(
                100,
                bids=[(100.0, 5.0)],
                asks=[(101.0, 2.0)],
                exchange_ts=BASE_TS,
                receive_ts=BASE_TS,
                process_ts=BASE_TS,
            )
        ]
        steps = 301
        for index in range(1, steps):
            timestamp = BASE_TS + index * 1_000
            events.append(
                depth_diff_event(
                    100 + index,
                    100 + index,
                    bids=[(100.0, 5.0 + index * 0.1)],
                    exchange_ts=timestamp,
                    receive_ts=timestamp,
                    process_ts=timestamp,
                )
            )

        states = feed_engine(engine, events)
        first, last = states[0], states[-1]

        self.assertFalse(first.quality.history_ready)
        self.assertIsNone(first.returns.return_5s)
        self.assertIsNone(first.volatility.realized_volatility_5s)

        self.assertEqual(last.quality.window_coverage_ms, 300_000)
        self.assertTrue(last.quality.history_ready)
        self.assertTrue(last.quality.feature_ready)
        self.assertTrue(last.quality.tradeable)
        self.assertEqual(last.quality.completeness, 1.0)
        self.assertIsNotNone(last.returns.return_300s)
        self.assertIsNotNone(last.returns.return_1s)
        self.assertIsNotNone(last.volatility.realized_volatility_60s)
        self.assertIsNotNone(last.flow.ofi_1s)
        self.assertEqual(last.flow.book_update_count, steps - 1)

    def test_trade_section_is_always_unavailable_without_trade_events(self) -> None:
        path = self.store_path()
        write_store(path, scenarios.reference_events())

        states = feed_engine(feature_engine(), ReplaySource(JsonlEventReader(path)).iter_events())

        for state in states:
            with self.subTest(ordinal=state.time.event_ordinal):
                self.assertFalse(state.trade.trade_stream_available)
                self.assertIsNone(state.trade.cvd)
                self.assertIsNone(state.trade.vwap)
                self.assertIsNone(state.trade.trade_count)

    def test_book_age_is_reported_and_age_threshold_is_honoured(self) -> None:
        path = self.store_path()
        write_store(path, scenarios.reference_events())

        states = feed_engine(feature_engine(max_book_age_ms=0), ReplaySource(JsonlEventReader(path)).iter_events())

        # 本阶段只有盘口事件，因此 age 恒为 0，阈值 0 仍然成立
        self.assertEqual(states[-1].quality.book_age_ms, 0)
        self.assertTrue(states[-1].quality.age_valid)
        # tradeable 仍受 warm-up 限制（本数据集事件时间跨度远小于 300s）
        self.assertFalse(states[-1].quality.history_ready)
        self.assertFalse(states[-1].quality.tradeable)


if __name__ == "__main__":
    unittest.main()
