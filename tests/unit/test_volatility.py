"""SC-8：已实现波动率的量纲与频率无关性。"""

from __future__ import annotations

import math
import unittest

from market.features.returns import PRICE_HISTORY_HORIZON_MS
from market.features.volatility import (
    UNAVAILABLE_VOLATILITY,
    VOLATILITY_WINDOWS_MS,
    compute_volatility,
    realized_volatility,
)
from market.features.windows import TimeSeries

T = 1_700_000_000_000
U = 0.001


def _series(observations: list[tuple[int, float]]) -> TimeSeries:
    series = TimeSeries(horizon_ms=PRICE_HISTORY_HORIZON_MS)
    for timestamp, mid in observations:
        series.record(timestamp, mid)
    return series


def _grid(*, step_ms: int, per_step_return: float, span_ms: int) -> TimeSeries:
    """按固定间隔生成等对数收益的价格路径。"""
    observations = []
    for index in range(span_ms // step_ms + 1):
        observations.append((T - span_ms + index * step_ms, 100.0 * math.exp(per_step_return * index)))
    return _series(observations)


class VolatilityDefinitionTest(unittest.TestCase):
    def test_schema_windows_are_fixed(self) -> None:
        self.assertEqual(VOLATILITY_WINDOWS_MS, (5_000, 15_000, 30_000, 60_000))

    def test_value_is_sum_of_squares_over_window_seconds(self) -> None:
        # 5s 窗口内 6 个观测 → 5 个对数收益，每个 = u
        series = _grid(step_ms=1_000, per_step_return=U, span_ms=5_000)
        value = realized_volatility(series, at=T, window_ms=5_000)

        assert value is not None
        self.assertAlmostEqual(value, math.sqrt(5 * U * U / 5), places=15)
        self.assertAlmostEqual(value, U, places=12)

    def test_unit_is_per_square_root_second(self) -> None:
        # 同样的对数收益放在 15s 窗口里 → 每秒波动率更小（除以更长的秒数）
        series = _grid(step_ms=1_000, per_step_return=U, span_ms=15_000)
        value_5s = realized_volatility(series, at=T, window_ms=5_000)
        value_15s = realized_volatility(series, at=T, window_ms=15_000)

        assert value_5s is not None and value_15s is not None
        self.assertAlmostEqual(value_5s, U, places=12)
        self.assertAlmostEqual(value_15s, math.sqrt(15 * U * U / 15), places=15)

    def test_sc8_independent_of_observation_frequency(self) -> None:
        """同样跨度、不同观测频率、每秒 Σr² 相同 → 结果相同。"""
        coarse = _grid(step_ms=1_000, per_step_return=U, span_ms=5_000)
        fine = _grid(step_ms=500, per_step_return=U / math.sqrt(2), span_ms=5_000)

        coarse_value = realized_volatility(coarse, at=T, window_ms=5_000)
        fine_value = realized_volatility(fine, at=T, window_ms=5_000)

        assert coarse_value is not None and fine_value is not None
        self.assertAlmostEqual(coarse_value, U, places=12)
        self.assertAlmostEqual(fine_value, U, places=12)
        self.assertAlmostEqual(coarse_value, fine_value, places=12)
        # 观测数量不同，但量纲不随频率改变
        self.assertEqual(coarse.count, 6)
        self.assertEqual(fine.count, 11)

    def test_sc8_more_observations_of_halved_return_keep_scale(self) -> None:
        base = _grid(step_ms=1_000, per_step_return=U, span_ms=30_000)
        denser = _grid(step_ms=250, per_step_return=U / 2, span_ms=30_000)

        base_value = realized_volatility(base, at=T, window_ms=30_000)
        denser_value = realized_volatility(denser, at=T, window_ms=30_000)

        assert base_value is not None and denser_value is not None
        self.assertAlmostEqual(base_value, denser_value, places=12)


class VolatilityAvailabilityTest(unittest.TestCase):
    def test_insufficient_history_is_none_not_zero(self) -> None:
        series = _grid(step_ms=1_000, per_step_return=U, span_ms=3_000)

        self.assertIsNone(realized_volatility(series, at=T, window_ms=5_000))
        self.assertIsNone(realized_volatility(series, at=T, window_ms=60_000))

    def test_single_observation_is_none(self) -> None:
        series = _series([(T, 100.0)])
        self.assertIsNone(realized_volatility(series, at=T, window_ms=5_000))

    def test_empty_history_is_none(self) -> None:
        self.assertIsNone(realized_volatility(_series([]), at=T, window_ms=5_000))

    def test_partial_coverage_is_none(self) -> None:
        # horizon 覆盖 5s，但窗口需要 60s
        series = _grid(step_ms=1_000, per_step_return=U, span_ms=5_000)
        volatility = compute_volatility(series, at=T)
        self.assertIsNotNone(volatility.realized_volatility_5s)
        self.assertIsNone(volatility.realized_volatility_60s)

    def test_unavailable_when_all_windows_lack_history(self) -> None:
        volatility = compute_volatility(_series([]), at=T)
        self.assertEqual(volatility, UNAVAILABLE_VOLATILITY)

    def test_non_positive_price_is_none(self) -> None:
        series = _series([(T - 5_000, 100.0), (T - 2_500, 0.0), (T, 100.0)])
        self.assertIsNone(realized_volatility(series, at=T, window_ms=5_000))


if __name__ == "__main__":
    unittest.main()
