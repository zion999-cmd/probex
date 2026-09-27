"""SC-9：warm-up 语义、质量闸门与「未知 ≠ 0」。"""

from __future__ import annotations

import unittest

from market.features.returns import HISTORY_WINDOW_MS
from market.health.state import BookHealth
from market.state.quality import build_data_quality
from tests.support import BASE_TS, depth_diff_event, depth_snapshot_event, feature_engine


def _quality(
    *,
    book_health: BookHealth = BookHealth.HEALTHY,
    book_age_ms: int | None = 0,
    coverage: int = HISTORY_WINDOW_MS,
    contiguous: bool = True,
    max_book_age_ms: int | None = None,
) -> object:
    return build_data_quality(
        book_health=book_health,
        book_age_ms=book_age_ms,
        sequence_contiguous=contiguous,
        window_coverage_ms=coverage,
        history_window_ms=HISTORY_WINDOW_MS,
        completeness=1.0,
        max_book_age_ms=max_book_age_ms,
    )


class DataQualityGateTest(unittest.TestCase):
    def test_history_threshold_is_the_longest_return_window(self) -> None:
        self.assertFalse(_quality(coverage=HISTORY_WINDOW_MS - 1).history_ready)  # type: ignore[attr-defined]
        self.assertTrue(_quality(coverage=HISTORY_WINDOW_MS).history_ready)  # type: ignore[attr-defined]

    def test_feature_ready_requires_healthy_book_and_history(self) -> None:
        self.assertTrue(_quality().feature_ready)  # type: ignore[attr-defined]
        self.assertFalse(_quality(book_health=BookHealth.STALE).feature_ready)  # type: ignore[attr-defined]
        self.assertFalse(_quality(coverage=0).feature_ready)  # type: ignore[attr-defined]

    def test_book_healthy_property(self) -> None:
        self.assertTrue(_quality().book_healthy)  # type: ignore[attr-defined]
        for health in (BookHealth.AWAITING_SNAPSHOT, BookHealth.STALE, BookHealth.RESYNCING):
            with self.subTest(health=health):
                self.assertFalse(_quality(book_health=health).book_healthy)  # type: ignore[attr-defined]

    def test_age_valid_requires_known_age(self) -> None:
        self.assertFalse(_quality(book_age_ms=None).age_valid)  # type: ignore[attr-defined]
        self.assertTrue(_quality(book_age_ms=123).age_valid)  # type: ignore[attr-defined]

    def test_age_threshold_applies_only_when_configured(self) -> None:
        self.assertTrue(_quality(book_age_ms=10_000, max_book_age_ms=None).age_valid)  # type: ignore[attr-defined]
        self.assertTrue(_quality(book_age_ms=10, max_book_age_ms=10).age_valid)  # type: ignore[attr-defined]
        self.assertFalse(_quality(book_age_ms=11, max_book_age_ms=10).age_valid)  # type: ignore[attr-defined]

    def test_tradeable_composition(self) -> None:
        self.assertTrue(_quality().tradeable)  # type: ignore[attr-defined]
        self.assertFalse(_quality(book_health=BookHealth.STALE).tradeable)  # type: ignore[attr-defined]
        self.assertFalse(_quality(coverage=0).tradeable)  # type: ignore[attr-defined]
        self.assertFalse(_quality(book_age_ms=None).tradeable)  # type: ignore[attr-defined]
        self.assertFalse(_quality(book_age_ms=50, max_book_age_ms=10).tradeable)  # type: ignore[attr-defined]

    def test_sequence_contiguous_does_not_gate_tradeable(self) -> None:
        quality = _quality(contiguous=False)
        self.assertFalse(quality.sequence_contiguous)  # type: ignore[attr-defined]
        self.assertTrue(quality.tradeable)  # type: ignore[attr-defined]


class WarmUpSemanticsTest(unittest.TestCase):
    """SC-9 验收：历史不足时 unavailable，而不是 0。"""

    def setUp(self) -> None:
        self.engine = feature_engine()
        self.state = self.engine.on_market_event(
            depth_snapshot_event(
                100,
                bids=[(100.0, 5.0)],
                asks=[(101.0, 2.0)],
                exchange_ts=BASE_TS,
                receive_ts=BASE_TS,
                process_ts=BASE_TS,
            )
        )

    def test_sc9_fresh_state_is_not_history_ready(self) -> None:
        self.assertEqual(self.state.quality.window_coverage_ms, 0)
        self.assertFalse(self.state.quality.history_ready)
        self.assertFalse(self.state.quality.feature_ready)
        self.assertFalse(self.state.quality.tradeable)

    def test_sc9_windowed_features_are_none_not_zero(self) -> None:
        self.assertIsNone(self.state.returns.return_1s)
        self.assertIsNone(self.state.returns.return_300s)
        self.assertIsNone(self.state.volatility.realized_volatility_5s)
        self.assertIsNone(self.state.flow.ofi_1s)
        self.assertIsNone(self.state.flow.normalized_ofi_1s)

    def test_instantaneous_features_are_available_immediately(self) -> None:
        self.assertEqual(self.state.price.mid, 100.5)
        self.assertEqual(self.state.depth.bid_depth_1, 5.0)
        self.assertEqual(self.state.quality.completeness, 27 / 45)

    def test_completeness_stays_within_unit_interval(self) -> None:
        self.assertGreaterEqual(self.state.quality.completeness, 0.0)
        self.assertLessEqual(self.state.quality.completeness, 1.0)

    def test_history_ready_is_reached_after_enough_event_time(self) -> None:
        engine = feature_engine()
        engine.on_market_event(depth_snapshot_event(100, bids=[(100.0, 5.0)], asks=[(101.0, 2.0)], exchange_ts=BASE_TS))
        engine.on_market_event(
            depth_diff_event(
                101,
                101,
                bids=[(100.0, 6.0)],
                exchange_ts=BASE_TS + 299_000,
                receive_ts=BASE_TS + 299_000,
                process_ts=BASE_TS + 299_000,
            )
        )
        state = engine.on_market_event(
            depth_diff_event(
                102,
                102,
                bids=[(100.0, 7.0)],
                exchange_ts=BASE_TS + HISTORY_WINDOW_MS,
                receive_ts=BASE_TS + HISTORY_WINDOW_MS,
                process_ts=BASE_TS + HISTORY_WINDOW_MS,
            )
        )

        self.assertEqual(state.quality.window_coverage_ms, HISTORY_WINDOW_MS)
        self.assertTrue(state.quality.history_ready)
        self.assertTrue(state.quality.feature_ready)
        self.assertTrue(state.quality.book_healthy)
        self.assertTrue(state.quality.tradeable)
        self.assertIsNotNone(state.returns.return_300s)
        self.assertIsNotNone(state.volatility.realized_volatility_60s)

    def test_window_with_single_observation_is_unavailable(self) -> None:
        engine = feature_engine()
        engine.on_market_event(depth_snapshot_event(100, bids=[(100.0, 5.0)], asks=[(101.0, 2.0)], exchange_ts=BASE_TS))
        # 历史跨过 300s，但 window 内只有 1 个观测 → 无法构成收益 → None
        state = engine.on_market_event(
            depth_diff_event(
                101,
                101,
                bids=[(100.0, 6.0)],
                exchange_ts=BASE_TS + HISTORY_WINDOW_MS,
                receive_ts=BASE_TS + HISTORY_WINDOW_MS,
                process_ts=BASE_TS + HISTORY_WINDOW_MS,
            )
        )

        self.assertTrue(state.quality.history_ready)
        self.assertIsNotNone(state.returns.return_300s)
        self.assertIsNone(state.volatility.realized_volatility_60s)

    def test_observed_zero_is_reported_as_zero(self) -> None:
        # 净 OFI 为 0 是「观测到的 0」，与「没有数据」不同
        state = self.engine.on_market_event(
            depth_diff_event(101, 101, bids=[(100.0, 5.0)], exchange_ts=BASE_TS + 1)
        )
        self.assertEqual(state.flow.event_ofi, 0.0)
        self.assertEqual(state.flow.ofi_1s, 0.0)


if __name__ == "__main__":
    unittest.main()
