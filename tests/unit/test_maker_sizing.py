"""P0001.7 单元测试：QuoteSize（有界乘数、fill probability 不进入数量、预算 fail closed）。"""

from __future__ import annotations

import unittest

from portfolio.types import Side
from strategy.maker.sizing import confidence_factor, plan_quote_size
from strategy.maker.types import QuoteTrigger
from tests.strategy_support import make_prediction, maker_config

BUDGET = 1_000.0


def _plan(*, side: Side = Side.BUY, config=None, price: float = 100.0, inventory_factor: float = 1.0, confidence: float = 0.9,
          budget: float | None = BUDGET, reduce_only: bool = False, position_qty: float = 0.0):
    return plan_quote_size(
        side=side,
        price=price,
        inventory_factor=inventory_factor,
        derived_confidence=confidence,
        remaining_risk_budget=budget,
        reduce_only=reduce_only,
        position_qty=position_qty,
        config=config if config is not None else maker_config(),
    )


class QuoteSizeTest(unittest.TestCase):
    def test_base_size_with_full_confidence(self) -> None:
        plan = _plan()

        self.assertTrue(plan.permitted)
        self.assertAlmostEqual(plan.quantity, 1.0)
        self.assertAlmostEqual(plan.confidence_factor, 1.0)
        self.assertAlmostEqual(plan.risk_budget_factor, 1.0)

    def test_sc5_fill_probability_never_changes_size(self) -> None:
        low_fill = make_prediction(buy_fill_probability=0.05, sell_fill_probability=0.05)
        high_fill = make_prediction(buy_fill_probability=0.95, sell_fill_probability=0.95)

        baseline = _plan()
        low = _plan(confidence=low_fill.derived_confidence)
        high = _plan(confidence=high_fill.derived_confidence)

        self.assertAlmostEqual(low.quantity, baseline.quantity)
        self.assertAlmostEqual(high.quantity, baseline.quantity)

    def test_confidence_factor_is_bounded_and_monotonic(self) -> None:
        config = maker_config()
        factors = [confidence_factor(derived_confidence=value, config=config) for value in (0.0, 0.2, 0.5, 0.8, 1.0)]

        self.assertEqual(factors, sorted(factors))
        self.assertAlmostEqual(factors[0], config.confidence_factor_min)  # 0 置信度 → 下界，而不是 0 量
        self.assertAlmostEqual(factors[-1], 1.0)

    def test_high_confidence_cannot_exceed_the_upper_bound(self) -> None:
        plan = _plan(confidence=1.0, inventory_factor=1.5)

        self.assertTrue(plan.permitted)
        self.assertLessEqual(plan.quantity, 1.5 + 1e-9)

    def test_quantity_is_floored_to_step(self) -> None:
        config = maker_config(quantity_step=0.1)

        plan = _plan(config=config, confidence=0.5)  # 0.75 → 0.7

        self.assertAlmostEqual(plan.quantity, 0.7)

    def test_size_below_minimum_is_blocked(self) -> None:
        config = maker_config(min_quote_size=1.0, confidence_factor_min=0.5)

        plan = _plan(config=config, confidence=0.0)

        self.assertFalse(plan.permitted)
        self.assertIs(plan.trigger, QuoteTrigger.SIZE_BELOW_MINIMUM)

    def test_unknown_budget_blocks_new_exposure(self) -> None:
        plan = _plan(budget=None)

        self.assertFalse(plan.permitted)
        self.assertIs(plan.trigger, QuoteTrigger.RISK_BUDGET_UNKNOWN)

    def test_exhausted_budget_blocks_new_exposure(self) -> None:
        plan = _plan(budget=0.0)

        self.assertFalse(plan.permitted)
        self.assertIs(plan.trigger, QuoteTrigger.RISK_BUDGET_EXHAUSTED)

    def test_small_budget_scales_down_but_keeps_the_floor(self) -> None:
        plan = _plan(budget=25.0)  # 25 / 100 = 0.25 == risk_factor_min

        self.assertTrue(plan.permitted)
        self.assertAlmostEqual(plan.risk_budget_factor, 0.25)
        self.assertAlmostEqual(plan.quantity, 0.25)

    def test_tiny_budget_uses_the_factor_floor_then_min_size(self) -> None:
        plan = _plan(budget=1.0)  # 0.01 → floor 0.25 → 0.25

        self.assertTrue(plan.permitted)
        self.assertAlmostEqual(plan.quantity, 0.25)

    def test_reduce_only_does_not_need_a_budget(self) -> None:
        plan = _plan(side=Side.SELL, budget=None, reduce_only=True, position_qty=2.0, confidence=0.5)

        self.assertTrue(plan.permitted)
        self.assertAlmostEqual(plan.quantity, 0.75)

    def test_reduce_only_is_capped_by_the_position(self) -> None:
        plan = _plan(side=Side.SELL, reduce_only=True, position_qty=0.4)

        self.assertTrue(plan.permitted)
        self.assertAlmostEqual(plan.quantity, 0.4)

    def test_reduce_only_with_nothing_to_reduce_is_blocked(self) -> None:
        plan = _plan(side=Side.SELL, reduce_only=True, position_qty=0.0)

        self.assertFalse(plan.permitted)
        self.assertIs(plan.trigger, QuoteTrigger.SIZE_BELOW_MINIMUM)

    def test_inventory_factor_bounds_are_respected(self) -> None:
        config = maker_config()
        plan = _plan(config=config, inventory_factor=99.0)  # 调用方传入越界值也不会放大到无限

        self.assertTrue(plan.permitted)
        self.assertAlmostEqual(plan.inventory_factor, config.size_factor_max)
        self.assertLessEqual(plan.quantity, config.size_factor_max + 1e-9)


if __name__ == "__main__":
    unittest.main()
