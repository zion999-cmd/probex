"""P0001.12 有界/降采样单测（SC-12）。"""

from __future__ import annotations

import unittest

from product.market_projection import MarketProjectionConfig, project_depth, project_trades
from product.market_timeline import project_timeline
from tests.unit.test_market_projection import config, snapshot, state


class MarketDownsamplingTest(unittest.TestCase):
    def test_display_bounds_are_mandatory(self) -> None:
        with self.assertRaises(TypeError):
            MarketProjectionConfig()  # type: ignore[call-arg]
        with self.assertRaises(ValueError):
            MarketProjectionConfig(window_ms=0, bucket_ms=1_000, max_points=10, price_levels=5)

    def test_timeline_keeps_the_last_state_per_bucket_and_is_capped(self) -> None:
        states = [state(ts) for ts in (1_000, 1_500, 2_100, 3_200, 4_400)]
        timeline = project_timeline(states, bucket_ms=1_000, max_points=3)

        self.assertEqual([point.ts for point in timeline.points], [2_100, 3_200, 4_400])
        self.assertEqual(timeline.source_points, 5)
        self.assertTrue(timeline.truncated)

    def test_timeline_window_trims_to_the_display_window(self) -> None:
        timeline = project_timeline([state(ts) for ts in (1_000, 10_000, 20_000)],
                                    bucket_ms=1_000, max_points=10, window_ms=10_000)
        self.assertEqual([point.ts for point in timeline.points], [10_000, 20_000])

    def test_depth_buckets_are_bounded_and_flagged(self) -> None:
        heatmap = project_depth([snapshot(ts) for ts in range(0, 20_000, 1_000)],
                                config=config(max_points=4, window_ms=1_000_000))
        buckets = {cell.bucket_ts for cell in heatmap.cells}

        self.assertLessEqual(len(buckets), 4)
        self.assertTrue(heatmap.truncated)
        self.assertLessEqual(len(heatmap.cells), 4 * config().price_levels * 2)

    def test_trades_are_capped_and_flagged(self) -> None:
        trades = [__import__("types").SimpleNamespace(ts=index, price=1.0, quantity=1.0, aggressor="buy")
                  for index in range(50)]
        projection = project_trades(trades, max_points=10)

        self.assertEqual(len(projection.prints), 10)
        self.assertTrue(projection.truncated)
        self.assertEqual(projection.source_points, 50)

    def test_no_unbounded_defaults_exist_in_the_projection_api(self) -> None:
        """SC-12：所有上限必须显式传入（不存在"无限加载全部 L2"的默认路径）。"""
        with self.assertRaises(TypeError):
            project_trades([], max_points=None)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            project_timeline([state(1_000)], bucket_ms=1_000, max_points=0)
