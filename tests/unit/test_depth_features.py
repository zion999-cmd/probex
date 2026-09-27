"""SC-4：多档深度与 imbalance 的公式 / 范围 / 缺失语义。"""

from __future__ import annotations

import unittest

from market.features.depth import (
    DEPTH_LEVELS,
    UNAVAILABLE_DEPTH_FEATURES,
    VAMP_LEVELS,
    compute_depth_features,
    imbalance,
)
from tests.support import book_view


def _levels(start: float, sizes: list[float], *, descending: bool) -> list[tuple[float, float]]:
    step = -1.0 if descending else 1.0
    return [(start + step * index, size) for index, size in enumerate(sizes)]


class DepthFeatureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.view = book_view(
            bids=_levels(100.0, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0], descending=True),
            asks=_levels(101.0, [2.0, 2.0, 2.0, 2.0, 2.0, 2.0], descending=False),
        )
        self.depth = compute_depth_features(self.view)

    def test_schema_levels_are_fixed(self) -> None:
        self.assertEqual(DEPTH_LEVELS, (1, 5, 10, 20))

    def test_top_n_depth_sums(self) -> None:
        self.assertEqual(self.depth.bid_depth_1, 1.0)
        self.assertEqual(self.depth.bid_depth_5, 15.0)
        self.assertEqual(self.depth.bid_depth_10, 21.0)  # 只有 6 档
        self.assertEqual(self.depth.bid_depth_20, 21.0)
        self.assertEqual(self.depth.ask_depth_1, 2.0)
        self.assertEqual(self.depth.ask_depth_5, 10.0)
        self.assertEqual(self.depth.ask_depth_20, 12.0)

    def test_imbalance_formula(self) -> None:
        self.assertEqual(self.depth.l1_imbalance, (1.0 - 2.0) / 3.0)
        self.assertEqual(self.depth.depth_imbalance_1, self.depth.l1_imbalance)
        self.assertEqual(self.depth.depth_imbalance_5, (15.0 - 10.0) / 25.0)
        self.assertEqual(self.depth.depth_imbalance_10, (21.0 - 12.0) / 33.0)
        self.assertEqual(self.depth.depth_imbalance_20, self.depth.depth_imbalance_10)

    def test_vamp_is_volume_weighted_mid_over_top_levels(self) -> None:
        # 双边前 5 档：
        #   bids: (100,1) (99,2) (98,3) (97,4) (96,5) → Σ price*size = 1460, Σ size = 15
        #   asks: (101,2) (102,2) (103,2) (104,2) (105,2) → Σ price*size = 1030, Σ size = 10
        # VAMP = 2490 / 25
        assert self.depth.vamp is not None
        self.assertAlmostEqual(self.depth.vamp, 2490.0 / 25.0)
        self.assertEqual(VAMP_LEVELS, 5)

    def test_imbalance_range_is_within_minus_one_and_one(self) -> None:
        for bid_size, ask_size in ((0.0, 5.0), (5.0, 0.0), (1.0, 1.0), (1000.0, 1.0), (1.0, 1000.0)):
            with self.subTest(bid_size=bid_size, ask_size=ask_size):
                value = imbalance(bid_size, ask_size)
                assert value is not None
                self.assertGreaterEqual(value, -1.0)
                self.assertLessEqual(value, 1.0)

    def test_imbalance_zero_denominator_is_unavailable(self) -> None:
        self.assertIsNone(imbalance(0.0, 0.0))

    def test_single_sided_imbalance_is_extreme(self) -> None:
        self.assertEqual(imbalance(5.0, 0.0), 1.0)
        self.assertEqual(imbalance(0.0, 5.0), -1.0)

    def test_empty_book_gives_zero_depth_and_unavailable_imbalance(self) -> None:
        depth = compute_depth_features(book_view())
        self.assertEqual(depth.bid_depth_1, 0.0)
        self.assertEqual(depth.ask_depth_20, 0.0)
        self.assertIsNone(depth.l1_imbalance)
        self.assertIsNone(depth.vamp)

    def test_unavailable_view_returns_all_none(self) -> None:
        depth = compute_depth_features(None)
        self.assertEqual(depth, UNAVAILABLE_DEPTH_FEATURES)
        for field in ("bid_depth_1", "ask_depth_20", "l1_imbalance", "vamp"):
            self.assertIsNone(getattr(depth, field))

    def test_vamp_is_unavailable_when_total_size_is_zero(self) -> None:
        self.assertIsNone(compute_depth_features(book_view()).vamp)


if __name__ == "__main__":
    unittest.main()
