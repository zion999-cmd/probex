"""SC-6：窗口基础设施的语义与「不依赖 wall-clock」。"""

from __future__ import annotations

import pathlib
import re
import unittest

from market.features.windows import EventWindow, TimeSeries, TimeWindow
from market.replay.clock import ReplayClock

T = 1_700_000_000_000

WINDOWS_SOURCE = pathlib.Path(__file__).resolve().parents[2] / "market" / "features" / "windows.py"


class TimeSeriesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.series = TimeSeries(horizon_ms=10_000)

    def test_record_and_lookup(self) -> None:
        self.series.record(T, 100.0)
        self.series.record(T + 1_000, 101.0)
        self.assertEqual(self.series.value_at_or_before(T), 100.0)
        self.assertEqual(self.series.value_at_or_before(T + 500), 100.0)
        self.assertEqual(self.series.value_at_or_before(T + 1_000), 101.0)
        self.assertEqual(self.series.value_at_or_before(T + 5_000), 101.0)

    def test_lookup_before_first_observation_is_none(self) -> None:
        self.series.record(T + 1_000, 100.0)
        self.assertIsNone(self.series.value_at_or_before(T))
        self.assertIsNone(self.series.value_at_or_before(T + 999))

    def test_out_of_order_observations_keep_timestamp_order(self) -> None:
        self.series.record(T + 2_000, 102.0)
        self.series.record(T, 100.0)
        self.series.record(T + 1_000, 101.0)

        self.assertEqual(self.series.oldest_timestamp, T)
        self.assertEqual(self.series.newest_timestamp, T + 2_000)
        self.assertEqual(self.series.value_at_or_before(T + 1_500), 101.0)
        self.assertEqual(self.series.observations_between(T, T + 2_000), (100.0, 101.0, 102.0))

    def test_observations_between_is_inclusive_on_both_ends(self) -> None:
        for offset in (0, 1_000, 2_000, 3_000):
            self.series.record(T + offset, float(offset))
        self.assertEqual(self.series.observations_between(T + 1_000, T + 2_000), (1000.0, 2000.0))

    def test_horizon_prunes_old_observations(self) -> None:
        self.series.record(T, 1.0)
        self.series.record(T + 10_000, 2.0)
        self.assertEqual(self.series.oldest_timestamp, T)
        self.series.record(T + 10_001, 3.0)
        # horizon = newest - 10_000 = T + 1，因此 T 处观测被剔除
        self.assertEqual(self.series.oldest_timestamp, T + 10_000)
        self.assertEqual(self.series.count, 2)

    def test_observation_older_than_horizon_is_dropped(self) -> None:
        self.series.record(T + 20_000, 5.0)
        self.assertFalse(self.series.record(T, 1.0))
        self.assertEqual(self.series.oldest_timestamp, T + 20_000)

    def test_duplicate_timestamp_keeps_latest_value(self) -> None:
        self.series.record(T, 1.0)
        self.series.record(T, 2.0)
        self.assertEqual(self.series.value_at_or_before(T), 2.0)
        self.assertEqual(self.series.count, 2)

    def test_coverage_and_reset(self) -> None:
        self.assertEqual(self.series.coverage_ms(T), 0)
        self.series.record(T, 1.0)
        self.series.record(T + 3_000, 2.0)
        self.assertEqual(self.series.coverage_ms(T + 3_000), 3_000)
        self.assertEqual(self.series.coverage_ms(T), 0)
        self.series.reset()
        self.assertEqual(self.series.count, 0)
        self.assertIsNone(self.series.oldest_timestamp)

    def test_invalid_inputs_rejected(self) -> None:
        with self.assertRaises(ValueError):
            TimeSeries(horizon_ms=0)
        for value in (-1, "1000", True, 1.5):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.series.record(value, 1.0)  # type: ignore[arg-type]
        for value in (float("nan"), float("inf"), "1.0", True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.series.record(T, value)  # type: ignore[arg-type]


class TimeWindowTest(unittest.TestCase):
    def test_sum_over_window(self) -> None:
        window = TimeWindow(1_000)
        window.observe(1.0, T)
        self.assertEqual(window.sum(T), 1.0)
        window.observe(2.0, T + 500)
        self.assertEqual(window.sum(T + 500), 3.0)
        window.observe(4.0, T + 1_500)

        # T 处观测已超出「最新观测 - 1000ms」的保留范围
        self.assertEqual(window.sum(T + 1_500), 6.0)
        self.assertEqual(window.count(T + 1_500), 2)

    def test_window_boundary_is_inclusive(self) -> None:
        window = TimeWindow(1_000)
        window.observe(5.0, T)
        self.assertEqual(window.sum(T + 1_000), 5.0)
        self.assertEqual(window.values(T), (5.0,))

    def test_empty_window_is_none_not_zero(self) -> None:
        window = TimeWindow(1_000)
        self.assertIsNone(window.sum(T))
        self.assertEqual(window.count(T), 0)

    def test_observed_zero_sums_to_zero(self) -> None:
        window = TimeWindow(1_000)
        window.observe(0.0, T)
        self.assertEqual(window.sum(T), 0.0)

    def test_reset(self) -> None:
        window = TimeWindow(1_000)
        window.observe(1.0, T)
        window.reset()
        self.assertIsNone(window.sum(T))


class EventWindowTest(unittest.TestCase):
    def test_keeps_most_recent_observations(self) -> None:
        window = EventWindow(capacity=2)
        for index in range(3):
            window.observe(float(index), T + index * 1_000)

        self.assertEqual(window.values(), (1.0, 2.0))
        self.assertEqual(window.count, 2)
        self.assertEqual(window.oldest_timestamp, T + 1_000)
        self.assertEqual(window.newest_timestamp, T + 2_000)
        self.assertEqual(window.coverage_ms(), 1_000)

    def test_empty_window_is_none_not_zero(self) -> None:
        window = EventWindow(capacity=3)
        self.assertIsNone(window.sum())
        self.assertIsNone(window.mean())

    def test_sum_and_mean(self) -> None:
        window = EventWindow(capacity=3)
        window.observe(1.0, T)
        window.observe(3.0, T + 1)
        self.assertEqual(window.sum(), 4.0)
        self.assertEqual(window.mean(), 2.0)

    def test_reset_and_validation(self) -> None:
        with self.assertRaises(ValueError):
            EventWindow(capacity=0)
        window = EventWindow(capacity=1)
        window.observe(1.0, T)
        window.reset()
        self.assertEqual(window.count, 0)
        self.assertEqual(window.coverage_ms(), 0)


class WindowIswallClockFreeTest(unittest.TestCase):
    """SC-6 验收：人工时间戳与 ReplayClock 驱动结果一致，且不依赖 wall-clock。"""

    def test_sc6_manual_timestamps_and_replay_clock_agree(self) -> None:
        samples = ((0.5, 0), (1.5, 400), (-0.5, 1_200), (2.0, 1_800))

        manual = TimeWindow(1_000)
        for value, offset in samples:
            manual.observe(value, T + offset)

        clock = ReplayClock()
        clocked = TimeWindow(1_000)
        for value, offset in samples:
            clock.advance_to(T + offset)
            clocked.observe(value, clock.now())

        self.assertEqual(manual.values(T + 1_800), clocked.values(T + 1_800))
        self.assertEqual(manual.sum(T + 1_800), clocked.sum(T + 1_800))
        self.assertEqual(manual.sum(T + 1_200), clocked.sum(T + 1_200))

    def test_sc6_repeated_driving_is_deterministic(self) -> None:
        def drive() -> tuple[float | None, ...]:
            window = TimeWindow(1_000)
            for offset in range(0, 5_000, 250):
                window.observe(offset / 1_000, T + offset)
            return tuple(window.sum(T + offset) for offset in range(0, 5_000, 250))

        self.assertEqual(drive(), drive())

    def test_sc6_window_module_does_not_import_time(self) -> None:
        source = WINDOWS_SOURCE.read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"^\s*(import time|from time import)", source, re.MULTILINE))


if __name__ == "__main__":
    unittest.main()
