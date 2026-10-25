"""资金费结算模型（FundingSettlementTracker）单元测试。

venue 语义：r>0 多头支付空头；支付额 = |持仓|名义 × r，结算点唯一一次。
"""

from __future__ import annotations

import unittest

from portfolio.funding_model import FundingModelError, FundingSettlementTracker

BOUNDARY = 1_700_000_000_000
RATE = 0.0001  # 1 bp
MARK = 60_000.0


class FundingSettlementTrackerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tracker = FundingSettlementTracker(asset="USDT", symbol="BTCUSDT")

    def test_long_pays_positive_rate_at_boundary(self) -> None:
        self.tracker.update(funding_rate=RATE, next_funding_time=BOUNDARY)
        self.assertIsNone(self.tracker.settle_due(
            now_ms=BOUNDARY - 1, position_qty=1.0, mark_price=MARK))
        payment = self.tracker.settle_due(
            now_ms=BOUNDARY, position_qty=1.0, mark_price=MARK)
        self.assertIsNotNone(payment)
        assert payment is not None
        self.assertAlmostEqual(payment.amount, -1.0 * MARK * RATE)
        self.assertEqual(payment.timestamp, BOUNDARY)
        self.assertEqual(payment.rate, RATE)
        self.assertEqual(payment.asset, "USDT")

    def test_short_receives_positive_rate(self) -> None:
        self.tracker.update(funding_rate=RATE, next_funding_time=BOUNDARY)
        payment = self.tracker.settle_due(
            now_ms=BOUNDARY, position_qty=-2.0, mark_price=MARK)
        assert payment is not None
        self.assertAlmostEqual(payment.amount, 2.0 * MARK * RATE)

    def test_negative_rate_reverses_direction(self) -> None:
        self.tracker.update(funding_rate=-RATE, next_funding_time=BOUNDARY)
        payment = self.tracker.settle_due(
            now_ms=BOUNDARY, position_qty=1.0, mark_price=MARK)
        assert payment is not None
        self.assertGreater(payment.amount, 0.0)

    def test_settlement_happens_at_most_once_per_boundary(self) -> None:
        self.tracker.update(funding_rate=RATE, next_funding_time=BOUNDARY)
        first = self.tracker.settle_due(
            now_ms=BOUNDARY, position_qty=1.0, mark_price=MARK)
        second = self.tracker.settle_due(
            now_ms=BOUNDARY + 5_000, position_qty=1.0, mark_price=MARK)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_flat_position_marks_boundary_without_payment(self) -> None:
        self.tracker.update(funding_rate=RATE, next_funding_time=BOUNDARY)
        self.assertIsNone(self.tracker.settle_due(
            now_ms=BOUNDARY, position_qty=0.0, mark_price=MARK))
        # 之后仓位变化也不再补收（结算点已过）
        self.assertIsNone(self.tracker.settle_due(
            now_ms=BOUNDARY + 1_000, position_qty=1.0, mark_price=MARK))

    def test_no_published_rate_means_no_settlement(self) -> None:
        self.assertIsNone(self.tracker.settle_due(
            now_ms=BOUNDARY, position_qty=1.0, mark_price=MARK))
        self.tracker.update(funding_rate=None, next_funding_time=BOUNDARY)
        self.assertIsNone(self.tracker.settle_due(
            now_ms=BOUNDARY, position_qty=1.0, mark_price=MARK))

    def test_new_boundary_after_settlement_settles_again(self) -> None:
        boundary_2 = BOUNDARY + 8 * 3_600 * 1_000
        self.tracker.update(funding_rate=RATE, next_funding_time=BOUNDARY)
        self.assertIsNotNone(self.tracker.settle_due(
            now_ms=BOUNDARY, position_qty=1.0, mark_price=MARK))
        self.tracker.update(funding_rate=RATE, next_funding_time=boundary_2)
        payment = self.tracker.settle_due(
            now_ms=boundary_2, position_qty=1.0, mark_price=MARK)
        self.assertIsNotNone(payment)

    def test_stale_boundary_announcement_does_not_replace_pending(self) -> None:
        self.tracker.update(funding_rate=RATE, next_funding_time=BOUNDARY)
        self.tracker.update(funding_rate=0.005, next_funding_time=BOUNDARY - 1_000)
        payment = self.tracker.settle_due(
            now_ms=BOUNDARY, position_qty=1.0, mark_price=MARK)
        assert payment is not None
        self.assertEqual(payment.rate, RATE)

    def test_invalid_rate_and_time_rejected(self) -> None:
        with self.assertRaises(FundingModelError):
            self.tracker.update(funding_rate=1.5, next_funding_time=BOUNDARY)
        with self.assertRaises(FundingModelError):
            self.tracker.update(funding_rate=RATE, next_funding_time=0)

    def test_settlement_requires_positive_mark(self) -> None:
        self.tracker.update(funding_rate=RATE, next_funding_time=BOUNDARY)
        with self.assertRaises(FundingModelError):
            self.tracker.settle_due(now_ms=BOUNDARY, position_qty=1.0, mark_price=0.0)


if __name__ == "__main__":
    unittest.main()
