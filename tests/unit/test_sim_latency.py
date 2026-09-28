"""P0001.8 单元测试：显式 submit / cancel 延迟模型（§9 / §10）。"""

from __future__ import annotations

import unittest

from execution.simulation.latency import LatencyModel
from execution.simulation.types import SimulationError
from tests.support import BASE_TS


class LatencyModelTest(unittest.TestCase):
    def test_values_are_required_and_validated(self) -> None:
        with self.assertRaises(SimulationError):
            LatencyModel(submit_latency_ms=-1, cancel_latency_ms=0)
        with self.assertRaises(SimulationError):
            LatencyModel(submit_latency_ms=0, cancel_latency_ms=-5)
        with self.assertRaises(SimulationError):
            LatencyModel(submit_latency_ms=True, cancel_latency_ms=0)  # type: ignore[arg-type]
        with self.assertRaises(SimulationError):
            LatencyModel(submit_latency_ms=1.5, cancel_latency_ms=0)  # type: ignore[arg-type]

    def test_zero_latency_is_explicit_and_effective_immediately(self) -> None:
        model = LatencyModel(submit_latency_ms=0, cancel_latency_ms=0)

        self.assertEqual(model.entry_ts(BASE_TS), BASE_TS)
        self.assertEqual(model.cancel_effective_ts(BASE_TS), BASE_TS)

    def test_entry_and_cancel_times_are_explicit_offsets(self) -> None:
        model = LatencyModel(submit_latency_ms=25, cancel_latency_ms=40)

        self.assertEqual(model.entry_ts(BASE_TS), BASE_TS + 25)
        self.assertEqual(model.cancel_effective_ts(BASE_TS), BASE_TS + 40)

    def test_timestamps_must_be_epoch_milliseconds(self) -> None:
        model = LatencyModel(submit_latency_ms=1, cancel_latency_ms=1)

        with self.assertRaises(SimulationError):
            model.entry_ts(-1)
        with self.assertRaises(SimulationError):
            model.cancel_effective_ts(1.5)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
