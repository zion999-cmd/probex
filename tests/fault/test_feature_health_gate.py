"""SC-2：盘口不健康时所有 book 派生 feature 必须显式 unavailable。"""

from __future__ import annotations

import unittest

from market.features.depth import UNAVAILABLE_DEPTH_FEATURES
from market.features.price import UNAVAILABLE_PRICE_FEATURES
from market.features.returns import UNAVAILABLE_RETURNS
from market.features.volatility import UNAVAILABLE_VOLATILITY
from market.health.state import BookHealth
from tests import scenarios
from tests.support import feed_engine, feature_engine


class FeatureHealthGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = feature_engine()
        self.healthy_states = feed_engine(self.engine, scenarios.events_before_gap())
        self.healthy_state = self.healthy_states[-1]

    def _gap_state(self):
        return self.engine.on_market_event(scenarios.delta_event(108))

    def test_sc2_gap_makes_book_features_unavailable(self) -> None:
        self.assertTrue(self.healthy_state.quality.book_healthy)
        self.assertIsNotNone(self.healthy_state.price.mid)

        state = self._gap_state()

        self.assertIs(state.quality.book_health, BookHealth.STALE)
        self.assertFalse(state.quality.book_healthy)
        self.assertEqual(state.price, UNAVAILABLE_PRICE_FEATURES)
        self.assertEqual(state.depth, UNAVAILABLE_DEPTH_FEATURES)
        self.assertEqual(state.returns, UNAVAILABLE_RETURNS)
        self.assertEqual(state.volatility, UNAVAILABLE_VOLATILITY)
        self.assertIsNone(state.flow.ofi_1s)
        self.assertIsNone(state.flow.ofi_5s)
        self.assertIsNone(state.flow.event_ofi)
        self.assertIsNone(state.flow.normalized_ofi_1s)

    def test_sc2_gates_are_closed_while_unhealthy(self) -> None:
        state = self._gap_state()

        self.assertFalse(state.quality.feature_ready)
        self.assertFalse(state.quality.tradeable)
        self.assertFalse(state.quality.history_ready is False and state.quality.book_healthy)

    def test_sc2_no_stale_reuse_of_previous_values(self) -> None:
        state = self._gap_state()

        # 不健康状态不能沿用上一个健康状态的数值
        self.assertIsNone(state.price.mid)
        self.assertIsNone(state.price.microprice)
        self.assertIsNone(state.depth.bid_depth_5)
        self.assertNotEqual(state.price, self.healthy_state.price)
        self.assertLess(state.quality.completeness, self.healthy_state.quality.completeness)

    def test_sc2_sequence_contiguous_becomes_sticky_false(self) -> None:
        self.assertTrue(self.healthy_state.quality.sequence_contiguous)
        self.assertFalse(self._gap_state().quality.sequence_contiguous)

    def test_sc2_unhealthy_state_keeps_identity_and_time(self) -> None:
        state = self._gap_state()

        self.assertEqual(state.identity.symbol, "BTCUSDT")
        self.assertEqual(state.time.as_of_exchange_ts, scenarios.delta_event(108).exchange_ts)
        self.assertEqual(state.feature_schema_version, "market-state-v1")

    def test_sc2_buffered_and_duplicate_events_do_not_leak_into_features(self) -> None:
        self._gap_state()
        state = self.engine.on_market_event(scenarios.delta_event(109))

        self.assertIs(state.quality.book_health, BookHealth.STALE)
        self.assertEqual(state.price, UNAVAILABLE_PRICE_FEATURES)
        self.assertIsNone(state.flow.event_ofi)

    def test_sc2_recovery_does_not_resurrect_pre_gap_values(self) -> None:
        before_gap_updates = self.healthy_state.flow.book_update_count
        self._gap_state()
        feed_engine(self.engine, scenarios.delta_events(109, 110))

        state = self.engine.on_market_event(scenarios.recovery_snapshot())

        self.assertIs(state.quality.book_health, BookHealth.HEALTHY)
        self.assertIsNotNone(state.price.mid)
        # OFI 窗口在失健康时被清空：恢复后必须重新 warm up，不能复活 gap 前的数值
        self.assertIsNone(state.flow.ofi_1s)
        self.assertIsNone(state.flow.event_ofi)
        # 计数是累计事实，不因失健康而被抹掉
        self.assertEqual(state.flow.book_update_count, before_gap_updates)
        self.assertFalse(state.quality.sequence_contiguous)

    def test_sc2_healthy_stream_reports_healthy(self) -> None:
        for state in self.healthy_states:
            with self.subTest(ordinal=state.time.event_ordinal):
                self.assertIs(state.quality.book_health, BookHealth.HEALTHY)

    def test_sc2_unsynced_engine_starts_unavailable(self) -> None:
        state = feature_engine().on_market_event(scenarios.delta_event(101))

        self.assertIs(state.quality.book_health, BookHealth.AWAITING_SNAPSHOT)
        self.assertEqual(state.price, UNAVAILABLE_PRICE_FEATURES)
        self.assertEqual(state.depth, UNAVAILABLE_DEPTH_FEATURES)


if __name__ == "__main__":
    unittest.main()
