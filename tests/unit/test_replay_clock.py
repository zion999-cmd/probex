"""ReplayClock 逻辑时钟测试。"""

from __future__ import annotations

import unittest

from market.replay.clock import ReplayClock
from tests.support import BASE_TS


class ReplayClockTest(unittest.TestCase):
    def test_initial_time_is_zero(self) -> None:
        self.assertEqual(ReplayClock().now(), 0)

    def test_custom_start(self) -> None:
        self.assertEqual(ReplayClock(start=BASE_TS).now(), BASE_TS)

    def test_advance_to_forward_moves_logical_time(self) -> None:
        clock = ReplayClock()
        self.assertEqual(clock.advance_to(BASE_TS), BASE_TS)
        self.assertEqual(clock.now(), BASE_TS)
        self.assertEqual(clock.advance_to(BASE_TS + 1), BASE_TS + 1)
        self.assertEqual(clock.now(), BASE_TS + 1)

    def test_equal_time_keeps_logical_time(self) -> None:
        clock = ReplayClock(start=BASE_TS)
        self.assertEqual(clock.advance_to(BASE_TS), BASE_TS)
        self.assertEqual(clock.now(), BASE_TS)

    def test_clock_never_goes_backwards(self) -> None:
        clock = ReplayClock(start=BASE_TS)
        self.assertEqual(clock.advance_to(BASE_TS - 500), BASE_TS)
        self.assertEqual(clock.now(), BASE_TS)

    def test_reset_returns_to_start(self) -> None:
        clock = ReplayClock(start=10)
        clock.advance_to(BASE_TS)
        clock.reset()
        self.assertEqual(clock.now(), 10)
        self.assertEqual(clock.advance_to(11), 11)

    def test_invalid_time_values_rejected(self) -> None:
        for value in (-1, "1", True, 1.5, None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    ReplayClock().advance_to(value)  # type: ignore[arg-type]

    def test_invalid_start_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ReplayClock(start=-1)

    def test_clock_does_not_use_wall_clock(self) -> None:
        # 逻辑时间与真实时间无关：起点为 0，跨越 1 小时也只按事件推进
        clock = ReplayClock()
        clock.advance_to(3_600_000)
        self.assertEqual(clock.now(), 3_600_000)

    def test_advance_returns_current_time(self) -> None:
        clock = ReplayClock()
        self.assertEqual(clock.advance_to(5), 5)
        self.assertEqual(clock.advance_to(3), 5)


if __name__ == "__main__":
    unittest.main()
