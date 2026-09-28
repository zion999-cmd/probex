"""P0001.9.4 SC-7 / SC-8：时钟校准事实（`ClockCalibration`）与校正延迟。"""

from __future__ import annotations

import unittest

from connectors.binance.private.auth import ClockCalibration, ServerTimeOffset
from connectors.binance.private.errors import PrivateAuthError
from connectors.binance.private.rest import PrivateRestClient
from tests.private_support import FakeRestFetcher, credentials
from tests.support import BASE_TS


class ClockCalibrationTest(unittest.TestCase):
    def test_sc7_sign_direction_exchange_ahead(self) -> None:
        """SC-7：交易所时钟快 200 ms、真实延迟 20 ms ⇒ raw = −180、corrected = +20。"""
        calibration = ClockCalibration(offset_ms=+200, round_trip_ms=40, uncertainty_ms=20, measured_at_ms=BASE_TS)

        raw = 1_020 - 1_200

        self.assertEqual(raw, -180)
        self.assertEqual(calibration.corrected_lag_ms(receive_ts=1_020, event_ts=1_200), +20)
        # 换算关系：本地接收时刻 + offset = 交易所时钟域
        self.assertEqual(1_020 + calibration.offset_ms, 1_220)

    def test_sc7_sign_direction_exchange_behind(self) -> None:
        """SC-7：交易所时钟慢 100 ms、真实延迟 30 ms ⇒ raw = +130、corrected = +30。"""
        calibration = ClockCalibration(offset_ms=-100, round_trip_ms=40, uncertainty_ms=20, measured_at_ms=BASE_TS)

        raw = 1_130 - 1_000

        self.assertEqual(raw, +130)
        self.assertEqual(calibration.corrected_lag_ms(receive_ts=1_130, event_ts=1_000), +30)

    def test_sc7_sign_direction_no_skew(self) -> None:
        """SC-7：无时钟偏差时 raw 即 corrected（25 ms）。"""
        calibration = ClockCalibration(offset_ms=0, round_trip_ms=0, uncertainty_ms=0, measured_at_ms=BASE_TS)

        self.assertEqual(calibration.corrected_lag_ms(receive_ts=1_025, event_ts=1_000), 25)

    def test_corrected_lag_uses_offset_additively(self) -> None:
        """符号回归：corrected = raw + offset（**不是** raw − offset）。"""
        calibration = ClockCalibration(offset_ms=500, round_trip_ms=60, uncertainty_ms=30, measured_at_ms=BASE_TS)

        raw = (BASE_TS + 1_000) - (BASE_TS + 1_030)

        self.assertEqual(raw, -30)
        self.assertEqual(
            calibration.corrected_lag_ms(receive_ts=BASE_TS + 1_000, event_ts=BASE_TS + 1_030),
            -30 + 500,
        )
        self.assertNotEqual(
            calibration.corrected_lag_ms(receive_ts=BASE_TS + 1_000, event_ts=BASE_TS + 1_030), -30 - 500
        )
        self.assertEqual(calibration.uncertainty_ms, 30)  # uncertainty 不被并入 corrected lag
        self.assertEqual(calibration.round_trip_ms, 60)

    def test_age_is_never_negative(self) -> None:
        calibration = ClockCalibration(offset_ms=0, round_trip_ms=0, uncertainty_ms=0, measured_at_ms=BASE_TS)

        self.assertEqual(calibration.age_ms(now_ms=BASE_TS + 10), 10)
        self.assertEqual(calibration.age_ms(now_ms=BASE_TS - 10), 0)

    def test_validation(self) -> None:
        with self.assertRaises(PrivateAuthError):
            ClockCalibration(offset_ms=0, round_trip_ms=10, uncertainty_ms=11, measured_at_ms=1)
        with self.assertRaises(PrivateAuthError):
            ClockCalibration(offset_ms=0, round_trip_ms=-1, uncertainty_ms=0, measured_at_ms=1)
        with self.assertRaises(PrivateAuthError):
            ClockCalibration(offset_ms=0, round_trip_ms=1, uncertainty_ms=0, measured_at_ms=-1)
        with self.assertRaises(PrivateAuthError):
            ClockCalibration(offset_ms=1.5, round_trip_ms=1, uncertainty_ms=0, measured_at_ms=1)  # type: ignore[arg-type]

    def test_measure_records_full_fact(self) -> None:
        offset = ServerTimeOffset()

        measured = offset.measure(server_time_ms=BASE_TS + 1_500, local_receive_ms=BASE_TS + 1_000, round_trip_ms=200)

        self.assertEqual(measured, 600)  # 500 + 200//2
        self.assertTrue(offset.measured)
        calibration = offset.calibration
        self.assertIsNotNone(calibration)
        self.assertEqual(calibration.offset_ms, 600)  # type: ignore[union-attr]
        self.assertEqual(calibration.round_trip_ms, 200)  # type: ignore[union-attr]
        self.assertEqual(calibration.uncertainty_ms, 100)  # type: ignore[union-attr]
        self.assertEqual(calibration.measured_at_ms, BASE_TS + 1_000)  # type: ignore[union-attr]

    def test_no_measurement_means_no_calibration(self) -> None:
        offset = ServerTimeOffset()

        self.assertIsNone(offset.calibration)
        self.assertFalse(offset.measured)


class RestClockMeasurementTest(unittest.TestCase):
    def _client(self, *, server_time: int, clock) -> PrivateRestClient:
        fetcher = FakeRestFetcher(responses={"/fapi/v1/time": {"serverTime": server_time}})
        return PrivateRestClient(credentials=credentials(), fetcher=fetcher, clock=clock)

    def test_measure_clock_returns_calibration_with_rtt(self) -> None:
        ticks = iter([BASE_TS, BASE_TS + 400])  # clock() 两次调用之间有 400ms RTT
        client = self._client(server_time=BASE_TS + 1_400, clock=lambda: next(ticks))

        calibration = client.measure_clock()

        self.assertEqual(calibration.round_trip_ms, 400)
        self.assertEqual(calibration.uncertainty_ms, 200)
        self.assertEqual(calibration.offset_ms, 1_400 - 400 + 200)  # server - local + rtt/2
        self.assertEqual(calibration.measured_at_ms, BASE_TS + 400)

    def test_measure_server_time_still_returns_offset(self) -> None:
        ticks = iter([BASE_TS, BASE_TS])
        client = self._client(server_time=BASE_TS + 500, clock=lambda: next(ticks))

        self.assertEqual(client.measure_server_time(), 500)
        self.assertEqual(client.offset.calibration.uncertainty_ms, 0)  # type: ignore[union-attr]

    def test_bad_server_time_fails_closed(self) -> None:
        from connectors.binance.private.errors import PrivateFormatError

        ticks = iter([BASE_TS, BASE_TS])
        client = self._client(server_time=0, clock=lambda: next(ticks))

        with self.assertRaises(PrivateFormatError):
            client.measure_clock()


if __name__ == "__main__":
    unittest.main()
