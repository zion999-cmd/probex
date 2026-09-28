"""P0001.7 单元测试：InventoryBias（SC-6 库存偏置、上下界、target_position）。"""

from __future__ import annotations

import unittest

from strategy.maker.inventory import compute_inventory_bias
from tests.strategy_support import maker_config


class InventoryBiasTest(unittest.TestCase):
    def test_flat_inventory_is_neutral(self) -> None:
        bias = compute_inventory_bias(position_qty=0.0, config=maker_config())

        self.assertAlmostEqual(bias.normalized_inventory, 0.0)
        self.assertAlmostEqual(bias.buy_size_factor, 1.0)
        self.assertAlmostEqual(bias.sell_size_factor, 1.0)
        self.assertEqual((bias.buy_extra_retreat_ticks, bias.sell_extra_retreat_ticks), (0, 0))

    def test_sc6_long_position_sells_more_and_buys_less(self) -> None:
        bias = compute_inventory_bias(position_qty=1.0, config=maker_config())

        self.assertGreater(bias.normalized_inventory, 0.0)
        self.assertLess(bias.buy_size_factor, 1.0)  # 买侧更保守
        self.assertGreater(bias.sell_size_factor, 1.0)  # 卖侧更积极
        self.assertGreater(bias.buy_extra_retreat_ticks, 0)  # 买侧多退
        self.assertEqual(bias.sell_extra_retreat_ticks, 0)

    def test_sc6_short_position_is_symmetric(self) -> None:
        long_bias = compute_inventory_bias(position_qty=1.0, config=maker_config())
        short_bias = compute_inventory_bias(position_qty=-1.0, config=maker_config())

        self.assertAlmostEqual(short_bias.buy_size_factor, long_bias.sell_size_factor)
        self.assertAlmostEqual(short_bias.sell_size_factor, long_bias.buy_size_factor)
        self.assertEqual(short_bias.sell_extra_retreat_ticks, long_bias.buy_extra_retreat_ticks)
        self.assertEqual(short_bias.buy_extra_retreat_ticks, 0)

    def test_normalized_inventory_is_clamped(self) -> None:
        bias = compute_inventory_bias(position_qty=50.0, config=maker_config(inventory_scale=1.0))

        self.assertAlmostEqual(bias.normalized_inventory, 1.0)

    def test_size_factors_respect_the_configured_bounds(self) -> None:
        config = maker_config(size_factor_min=0.5, size_factor_max=1.2, inventory_size_strength=1.0)
        bias = compute_inventory_bias(position_qty=10.0, config=config)

        self.assertAlmostEqual(bias.buy_size_factor, 0.5)
        self.assertAlmostEqual(bias.sell_size_factor, 1.2)

    def test_retreat_ticks_respect_the_configured_bound(self) -> None:
        config = maker_config(inventory_retreat_ticks_max=1)
        bias = compute_inventory_bias(position_qty=1.0, config=config)

        self.assertEqual(bias.buy_extra_retreat_ticks, 1)

    def test_target_position_offsets_the_neutral_point(self) -> None:
        config = maker_config(target_position=2.0)
        bias = compute_inventory_bias(position_qty=2.0, config=config)

        self.assertAlmostEqual(bias.normalized_inventory, 0.0)
        self.assertAlmostEqual(bias.buy_size_factor, 1.0)
        self.assertAlmostEqual(bias.sell_size_factor, 1.0)

    def test_tiny_inventory_does_not_round_up_to_a_tick(self) -> None:
        bias = compute_inventory_bias(position_qty=0.05, config=maker_config(inventory_scale=1.0))

        self.assertGreater(bias.normalized_inventory, 0.0)
        self.assertEqual(bias.buy_extra_retreat_ticks, 0)  # 0.05 × 2 = 0.1 → 0

    def test_detail_is_deterministic(self) -> None:
        first = compute_inventory_bias(position_qty=1.234, config=maker_config())
        second = compute_inventory_bias(position_qty=1.234, config=maker_config())

        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
