"""P0001.8 单元测试：Maker 队列近似（§3 – §6、§8）。"""

from __future__ import annotations

import unittest

from execution.simulation.queue import UNKNOWN_QUEUE, QueueEstimate, clear_queue, consume_queue, queue_from_visible
from execution.simulation.types import QueueState, SimulationError


class QueueEstimateTest(unittest.TestCase):
    def test_known_requires_ahead(self) -> None:
        with self.assertRaises(SimulationError):
            QueueEstimate(state=QueueState.KNOWN)
        with self.assertRaises(SimulationError):
            QueueEstimate(state=QueueState.KNOWN, ahead=-1.0)

    def test_unknown_must_not_carry_ahead(self) -> None:
        with self.assertRaises(SimulationError):
            QueueEstimate(state=QueueState.UNKNOWN, ahead=0.0)

    def test_state_must_be_a_queue_state(self) -> None:
        with self.assertRaises(SimulationError):
            QueueEstimate(state="known")  # type: ignore[arg-type]


class QueueFromVisibleTest(unittest.TestCase):
    def test_sc1_visible_size_becomes_the_queue_ahead(self) -> None:
        queue = queue_from_visible(3.0)

        self.assertIs(queue.state, QueueState.KNOWN)
        self.assertEqual(queue.ahead, 3.0)

    def test_sc9_unobservable_level_is_unknown_not_zero(self) -> None:
        for visible in (None, 0.0, -1.0, float("nan"), float("inf")):
            with self.subTest(visible=visible):
                queue = queue_from_visible(visible)

                self.assertIs(queue.state, QueueState.UNKNOWN)
                self.assertIsNone(queue.ahead)
                self.assertTrue(queue.is_unknown)


class QueueConsumptionTest(unittest.TestCase):
    def test_fill_arithmetic_matches_the_contract_example(self) -> None:
        """§5 的例子逐条复现：queue 3 / our 1。"""
        queue = queue_from_visible(3.0)
        expected = [(1.0, 2.0, 0.0), (2.0, 0.0, 0.0), (0.4, 0.0, 0.4), (0.6, 0.0, 0.6)]

        for volume, ahead, fillable in expected:
            with self.subTest(volume=volume):
                queue, amount = consume_queue(queue, volume)
                self.assertEqual(queue.ahead, ahead)
                self.assertAlmostEqual(amount, fillable)

    def test_partial_consumption_leaves_the_queue_positive(self) -> None:
        queue, fillable = consume_queue(queue_from_visible(2.5), 1.0)

        self.assertAlmostEqual(queue.ahead or 0.0, 1.5)
        self.assertEqual(fillable, 0.0)

    def test_unknown_queue_never_consumes_and_never_fills(self) -> None:
        queue, fillable = consume_queue(UNKNOWN_QUEUE, 10.0)

        self.assertIs(queue.state, QueueState.UNKNOWN)
        self.assertEqual(fillable, 0.0)

    def test_negative_volume_is_rejected(self) -> None:
        with self.assertRaises(SimulationError):
            consume_queue(queue_from_visible(1.0), -1.0)

    def test_zero_volume_is_a_no_op(self) -> None:
        queue, fillable = consume_queue(queue_from_visible(1.0), 0.0)

        self.assertEqual(queue.ahead, 1.0)
        self.assertEqual(fillable, 0.0)

    def test_sc6_clear_queue_marks_the_level_as_swept(self) -> None:
        cleared = clear_queue(queue_from_visible(5.0))

        self.assertIs(cleared.state, QueueState.KNOWN)
        self.assertEqual(cleared.ahead, 0.0)

    def test_clearing_an_unknown_queue_is_rejected(self) -> None:
        with self.assertRaises(SimulationError):
            clear_queue(UNKNOWN_QUEUE)


if __name__ == "__main__":
    unittest.main()
