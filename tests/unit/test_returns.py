"""SC-7：收益窗口的 lookup 规则（永不读取未来）。"""

from __future__ import annotations

import unittest

from market.features.returns import (
    HISTORY_WINDOW_MS,
    PRICE_HISTORY_HORIZON_MS,
    RETURN_WINDOWS_MS,
    UNAVAILABLE_RETURNS,
    compute_returns,
    log_returns,
)
from market.features.windows import TimeSeries

T = 1_700_000_000_000


def _history(observations: list[tuple[int, float]]) -> TimeSeries:
    series = TimeSeries(horizon_ms=PRICE_HISTORY_HORIZON_MS)
    for timestamp, mid in observations:
        series.record(timestamp, mid)
    return series


class ReturnWindowTest(unittest.TestCase):
    def test_schema_windows_are_fixed(self) -> None:
        self.assertEqual(RETURN_WINDOWS_MS, (1_000, 3_000, 5_000, 15_000, 30_000, 60_000, 300_000))
        self.assertEqual(HISTORY_WINDOW_MS, 300_000)
        self.assertEqual(PRICE_HISTORY_HORIZON_MS, 600_000)

    def test_return_uses_latest_observation_at_or_before_target(self) -> None:
        history = _history([(T, 100.0), (T + 500, 105.0), (T + 1_200, 110.0)])
        returns = compute_returns(history, at=T + 5_000, current_mid=120.0)

        # return_5s → target T（latest <= T 为 100.0）；return_3s → target T+2000（latest <= 为 T+1200 的 110.0）
        self.assertAlmostEqual(returns.return_5s or 0.0, 120.0 / 100.0 - 1.0)
        self.assertAlmostEqual(returns.return_3s or 0.0, 120.0 / 110.0 - 1.0)

    def test_never_reads_observations_after_target(self) -> None:
        history = _history([(T, 100.0), (T + 1_000, 101.0), (T + 6_000, 999.0)])
        returns = compute_returns(history, at=T + 10_000, current_mid=110.0)

        # return_5s 的 target = T+5000：必须落在 T+1000（不能用 T+6000 的 999.0）
        self.assertAlmostEqual(returns.return_5s or 0.0, 110.0 / 101.0 - 1.0)
        # return_1s 的 target = T+9000：落在 T+6000
        self.assertAlmostEqual(returns.return_1s or 0.0, 110.0 / 999.0 - 1.0)

    def test_observation_exactly_after_boundary_is_ignored(self) -> None:
        history = _history([(T, 100.0), (T + 5_001, 999.0)])
        returns = compute_returns(history, at=T + 10_000, current_mid=110.0)

        # target = T+5000：T+5001 在之后，T 在之前
        self.assertAlmostEqual(returns.return_5s or 0.0, 110.0 / 100.0 - 1.0)

    def test_observation_exactly_at_boundary_is_used(self) -> None:
        history = _history([(T, 100.0), (T + 5_000, 200.0)])
        returns = compute_returns(history, at=T + 10_000, current_mid=110.0)

        self.assertAlmostEqual(returns.return_5s or 0.0, 110.0 / 200.0 - 1.0)

    def test_missing_history_is_none_not_zero(self) -> None:
        history = _history([(T + 9_000, 100.0)])
        returns = compute_returns(history, at=T + 10_000, current_mid=101.0)

        self.assertIsNone(returns.return_5s)
        self.assertIsNone(returns.return_15s)
        self.assertAlmostEqual(returns.return_1s or 0.0, 101.0 / 100.0 - 1.0)

    def test_all_windows_available_with_long_history(self) -> None:
        history = _history([(T - 300_000, 100.0), (T, 110.0)])
        returns = compute_returns(history, at=T, current_mid=110.0)

        self.assertAlmostEqual(returns.return_300s or 0.0, 110.0 / 100.0 - 1.0)
        self.assertAlmostEqual(returns.return_1s or 0.0, 110.0 / 100.0 - 1.0)

    def test_unavailable_current_mid_makes_all_returns_none(self) -> None:
        history = _history([(T, 100.0)])
        self.assertEqual(compute_returns(history, at=T, current_mid=None), UNAVAILABLE_RETURNS)

    def test_zero_past_mid_is_unavailable(self) -> None:
        history = _history([(T, 0.0)])
        returns = compute_returns(history, at=T + 10_000, current_mid=100.0)
        self.assertIsNone(returns.return_1s)

    def test_lookup_rule_is_documented_by_implementation(self) -> None:
        # 事件驱动数据没有恰好 t-h 的观测：必须回退到更早的观测，而不是跳过窗口
        history = _history([(T, 50.0), (T + 4_500, 100.0)])
        returns = compute_returns(history, at=T + 10_000, current_mid=200.0)
        self.assertAlmostEqual(returns.return_5s or 0.0, 200.0 / 100.0 - 1.0)


class LogReturnsTest(unittest.TestCase):
    def test_log_returns_of_consecutive_observations(self) -> None:
        import math

        values = (100.0, 110.0, 99.0)
        expected = (math.log(1.1), math.log(0.9))
        actual = log_returns(values)
        assert actual is not None
        for got, want in zip(actual, expected):
            self.assertAlmostEqual(got, want)

    def test_single_observation_has_no_return(self) -> None:
        self.assertIsNone(log_returns((100.0,)))
        self.assertIsNone(log_returns(()))

    def test_non_positive_price_is_unavailable(self) -> None:
        self.assertIsNone(log_returns((100.0, 0.0)))


if __name__ == "__main__":
    unittest.main()
