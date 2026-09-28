"""P0001.8 单元测试：显式 FeeSchedule（§12）。"""

from __future__ import annotations

import unittest

from execution.simulation.fees import FeeSchedule
from execution.simulation.types import SimulationError


class FeeScheduleTest(unittest.TestCase):
    def test_rate_and_asset_are_required(self) -> None:
        with self.assertRaises(SimulationError):
            FeeSchedule(maker_fee_rate=-0.0001, fee_asset="USDT")
        with self.assertRaises(SimulationError):
            FeeSchedule(maker_fee_rate=True, fee_asset="USDT")  # type: ignore[arg-type]
        with self.assertRaises(SimulationError):
            FeeSchedule(maker_fee_rate=float("nan"), fee_asset="USDT")
        with self.assertRaises(SimulationError):
            FeeSchedule(maker_fee_rate=0.0001, fee_asset="")

    def test_zero_fee_is_explicit(self) -> None:
        schedule = FeeSchedule(maker_fee_rate=0.0, fee_asset="USDT")

        self.assertEqual(schedule.maker_fee(price=100.0, quantity=2.0), 0.0)

    def test_maker_fee_is_price_times_quantity_times_rate(self) -> None:
        schedule = FeeSchedule(maker_fee_rate=0.0002, fee_asset="USDT")

        self.assertAlmostEqual(schedule.maker_fee(price=100.0, quantity=0.4), 0.008)

    def test_fee_asset_is_carried_through(self) -> None:
        schedule = FeeSchedule(maker_fee_rate=0.001, fee_asset="USDC")

        self.assertEqual(schedule.fee_asset, "USDC")
        self.assertAlmostEqual(schedule.maker_fee(price=10.0, quantity=1.0), 0.01)

    def test_invalid_fee_inputs_are_rejected(self) -> None:
        schedule = FeeSchedule(maker_fee_rate=0.0002, fee_asset="USDT")

        with self.assertRaises(SimulationError):
            schedule.maker_fee(price=0.0, quantity=1.0)
        with self.assertRaises(SimulationError):
            schedule.maker_fee(price=100.0, quantity=-1.0)
        with self.assertRaises(SimulationError):
            schedule.maker_fee(price=100.0, quantity=float("inf"))


if __name__ == "__main__":
    unittest.main()
