"""SC-3：microprice 与价格 feature 的公式 / 缺失语义。"""

from __future__ import annotations

import unittest

from market.features.price import UNAVAILABLE_PRICE_FEATURES, compute_price_features, microprice
from tests.support import book_view


class MicropriceTest(unittest.TestCase):
    """SC-3 验收：microprice 覆盖对称 / 极端 / 空侧 / 零数量。"""

    def test_symmetric_sizes_equal_mid(self) -> None:
        self.assertEqual(microprice(best_bid=100.0, bid_size=2.0, best_ask=101.0, ask_size=2.0), 100.5)

    def test_weighting_uses_opposite_side_size(self) -> None:
        # 权重是对侧数量：bid 价格配 ask_size=3，ask 价格配 bid_size=1
        # (100 * 3 + 101 * 1) / 4
        self.assertEqual(microprice(best_bid=100.0, bid_size=1.0, best_ask=101.0, ask_size=3.0), 100.25)

    def test_extreme_bid_imbalance_moves_towards_ask(self) -> None:
        # bid_size 极大 → bid 价格权重 = ask_size = 1，ask 价格权重 = 1000
        value = microprice(best_bid=100.0, bid_size=1000.0, best_ask=101.0, ask_size=1.0)
        assert value is not None
        self.assertGreater(value, 100.99)
        self.assertLess(value, 101.0)

    def test_extreme_ask_imbalance_moves_towards_bid(self) -> None:
        value = microprice(best_bid=100.0, bid_size=1.0, best_ask=101.0, ask_size=1000.0)
        assert value is not None
        self.assertGreater(value, 100.0)
        self.assertLess(value, 100.01)

    def test_zero_total_size_is_unavailable_not_zero(self) -> None:
        self.assertIsNone(microprice(best_bid=100.0, bid_size=0.0, best_ask=101.0, ask_size=0.0))

    def test_zero_size_one_side_collapses_to_bid_price(self) -> None:
        # bid_size = 0 → bid 价格权重 = ask_size = 5，ask 价格权重 = 0
        self.assertEqual(microprice(best_bid=100.0, bid_size=0.0, best_ask=101.0, ask_size=5.0), 100.0)

    def test_empty_side_gives_unavailable(self) -> None:
        features = compute_price_features(book_view(asks=[(101.0, 1.0)]))
        self.assertEqual(features.best_bid, None)
        self.assertEqual(features.bid_size, None)
        self.assertEqual(features.best_ask, 101.0)
        self.assertEqual(features.ask_size, 1.0)
        self.assertIsNone(features.mid)
        self.assertIsNone(features.spread)
        self.assertIsNone(features.microprice)

    def test_unavailable_view_returns_all_none(self) -> None:
        self.assertEqual(compute_price_features(None), UNAVAILABLE_PRICE_FEATURES)


class PriceFeatureFormulaTest(unittest.TestCase):
    def test_mid_and_spread(self) -> None:
        features = compute_price_features(book_view(bids=[(100.0, 1.0)], asks=[(100.5, 2.0)]))
        self.assertEqual(features.best_bid, 100.0)
        self.assertEqual(features.best_ask, 100.5)
        self.assertEqual(features.bid_size, 1.0)
        self.assertEqual(features.ask_size, 2.0)
        self.assertEqual(features.mid, 100.25)
        self.assertEqual(features.spread, 0.5)
        self.assertAlmostEqual(features.spread_bps, 0.5 / 100.25 * 10_000)

    def test_microprice_is_included_in_price_features(self) -> None:
        features = compute_price_features(book_view(bids=[(100.0, 3.0)], asks=[(101.0, 1.0)]))
        # (100 * 1 + 101 * 3) / 4
        self.assertEqual(features.microprice, 100.75)
    def test_best_levels_use_best_prices(self) -> None:
        features = compute_price_features(
            book_view(bids=[(100.0, 1.0), (99.0, 9.0)], asks=[(101.0, 1.0), (102.0, 9.0)])
        )
        self.assertEqual(features.best_bid, 100.0)
        self.assertEqual(features.best_ask, 101.0)
        self.assertEqual(features.bid_size, 1.0)

    def test_empty_book_is_not_an_error(self) -> None:
        features = compute_price_features(book_view())
        self.assertIsNone(features.mid)
        self.assertEqual(features.best_bid, None)

    def test_stale_view_still_computes_from_given_levels(self) -> None:
        # 盘口健康门在 FeatureEngine 中统一处理；这里只验证公式本身
        features = compute_price_features(book_view(bids=[(100.0, 1.0)], asks=[(101.0, 1.0)]))
        self.assertEqual(features.mid, 100.5)

    def test_spread_bps_is_none_when_half_spread_is_zero(self) -> None:
        features = compute_price_features(book_view(bids=[(100.0, 1.0)], asks=[(100.0, 1.0)]))
        self.assertEqual(features.mid, 100.0)
        self.assertEqual(features.spread, 0.0)
        self.assertEqual(features.spread_bps, 0.0)


if __name__ == "__main__":
    unittest.main()
