"""P0001.7 单元测试：QuotePrice（SC-7 永不穿价、SC-8 cost floor、后退原因、tick 网格）。"""

from __future__ import annotations

import unittest

from portfolio.types import Side
from strategy.maker.pricing import plan_quote_price
from strategy.maker.types import QuoteTrigger
from tests.strategy_support import make_prediction, maker_config, market_state


def _plan(side: Side, *, config=None, state=None, prediction=None, reduce_only=False, extra=0):
    return plan_quote_price(
        side=side,
        state=state if state is not None else market_state(),
        prediction=prediction,
        config=config if config is not None else maker_config(),
        reduce_only=reduce_only,
        extra_retreat_ticks=extra,
    )


class QuotePriceTest(unittest.TestCase):
    def test_sc1_defaults_to_best_bid_and_best_ask(self) -> None:
        bid = _plan(Side.BUY)
        ask = _plan(Side.SELL)

        self.assertTrue(bid.permitted)
        self.assertEqual(bid.price, 100.0)
        self.assertEqual(bid.retreat_ticks, 0)
        self.assertEqual(ask.price, 101.0)
        self.assertEqual(ask.retreat_ticks, 0)
        self.assertAlmostEqual(bid.edge_bps, 49.7512, places=3)

    def test_sc7_never_crosses_the_opposite_side(self) -> None:
        for state in (
            market_state(),
            market_state(best_bid=100.15, best_ask=100.16),
            market_state(microprice=99.0, imbalance_5=-0.9),
            market_state(best_bid=100.0, best_ask=100.02),
        ):
            for config in (maker_config(), maker_config(max_back_ticks=0, minimum_edge_bps=0.0)):
                with self.subTest(bid=state.price.best_bid, ask=state.price.best_ask):
                    bid = _plan(Side.BUY, state=state, config=config, prediction=make_prediction())
                    ask = _plan(Side.SELL, state=state, config=config, prediction=make_prediction())
                    if bid.permitted:
                        self.assertLessEqual(bid.price, state.price.best_bid)
                    if ask.permitted:
                        self.assertGreaterEqual(ask.price, state.price.best_ask)

    def test_prices_are_aligned_to_the_tick_grid(self) -> None:
        state = market_state(best_bid=100.123, best_ask=100.987)
        bid = _plan(Side.BUY, state=state, prediction=make_prediction(), extra=1)
        ask = _plan(Side.SELL, state=state, prediction=make_prediction(), extra=1)

        for price in (bid.price, ask.price):
            self.assertAlmostEqual(price / 0.01, round(price / 0.01), places=6)
        self.assertAlmostEqual(bid.price, 100.11, places=9)  # 向下取整
        self.assertAlmostEqual(ask.price, 101.0, places=9)  # 向上取整

    def test_adverse_selection_retreats_one_tick(self) -> None:
        prediction = make_prediction(buy_adverse_selection=0.45, sell_adverse_selection=0.1)

        bid = _plan(Side.BUY, prediction=prediction)
        ask = _plan(Side.SELL, prediction=prediction)

        self.assertEqual(bid.retreat_ticks, 1)
        self.assertAlmostEqual(bid.price, 99.99, places=9)
        self.assertIn("adverse_selection", " ".join(bid.reasons))
        self.assertEqual(ask.retreat_ticks, 0)

    def test_adverse_selection_block_forbids_the_side(self) -> None:
        prediction = make_prediction(buy_adverse_selection=0.75)

        bid = _plan(Side.BUY, prediction=prediction)

        self.assertFalse(bid.permitted)
        self.assertIsNone(bid.price)
        self.assertIs(bid.trigger, QuoteTrigger.ADVERSE_SELECTION)

    def test_imbalance_and_microprice_skew_retreat(self) -> None:
        state = market_state(imbalance_5=-0.8, microprice=99.5)

        bid = _plan(Side.BUY, state=state, prediction=make_prediction())
        ask = _plan(Side.SELL, state=state, prediction=make_prediction())

        self.assertEqual(bid.retreat_ticks, 2)  # 失衡不利 + skew 不利
        self.assertEqual(ask.retreat_ticks, 0)
        self.assertAlmostEqual(bid.price, 99.98, places=9)

    def test_predicted_move_against_retreats(self) -> None:
        prediction = make_prediction(strong_down=0.3, down=0.3, flat=0.2, up=0.1, strong_up=0.1)

        bid = _plan(Side.BUY, prediction=prediction)
        ask = _plan(Side.SELL, prediction=prediction)

        self.assertEqual(bid.retreat_ticks, 1)
        self.assertEqual(ask.retreat_ticks, 0)

    def test_retreat_is_capped_by_max_back_ticks(self) -> None:
        state = market_state(imbalance_5=-0.9, microprice=99.0)
        prediction = make_prediction(buy_adverse_selection=0.5, strong_down=0.3, down=0.3, flat=0.2, up=0.1, strong_up=0.1)

        bid = _plan(Side.BUY, state=state, prediction=prediction, config=maker_config(max_back_ticks=1))

        self.assertEqual(bid.retreat_ticks, 1)
        self.assertAlmostEqual(bid.price, 99.99, places=9)

    def test_sc8_cost_floor_unmet_when_spread_is_narrow(self) -> None:
        state = market_state(best_bid=100.0, best_ask=100.01)
        config = maker_config(minimum_edge_bps=100.0, max_back_ticks=0)

        bid = _plan(Side.BUY, state=state, config=config, prediction=make_prediction())

        self.assertFalse(bid.permitted)
        self.assertIs(bid.trigger, QuoteTrigger.COST_FLOOR_UNMET)

    def test_cost_floor_is_met_by_retreating_more_ticks(self) -> None:
        state = market_state(best_bid=100.0, best_ask=100.01)
        config = maker_config(minimum_edge_bps=1.0)
        baseline = _plan(Side.BUY, state=state, config=maker_config(minimum_edge_bps=0.0), prediction=make_prediction())

        bid = _plan(Side.BUY, state=state, config=config, prediction=make_prediction())

        self.assertEqual(baseline.retreat_ticks, 0)  # spread 自身不满足 floor
        self.assertLess(baseline.edge_bps, 1.0)
        self.assertTrue(bid.permitted)
        self.assertGreaterEqual(bid.retreat_ticks, 1)
        self.assertGreaterEqual(bid.edge_bps + 1e-9, 1.0)

    def test_cost_floor_gives_up_when_max_back_ticks_is_not_enough(self) -> None:
        state = market_state(best_bid=100.0, best_ask=100.01)
        config = maker_config(minimum_edge_bps=500.0, max_back_ticks=2)

        bid = _plan(Side.BUY, state=state, config=config, prediction=make_prediction())

        self.assertFalse(bid.permitted)
        self.assertIs(bid.trigger, QuoteTrigger.COST_FLOOR_UNMET)
        self.assertEqual(bid.retreat_ticks, 2)

    def test_reduce_only_skips_the_cost_floor_but_not_passivity(self) -> None:
        state = market_state(best_bid=100.0, best_ask=100.01)
        config = maker_config(minimum_edge_bps=500.0)

        ask = _plan(Side.SELL, state=state, config=config, reduce_only=True)

        self.assertTrue(ask.permitted)
        self.assertGreaterEqual(ask.price, state.price.best_ask)

    def test_missing_book_blocks_the_side(self) -> None:
        state = market_state(best_bid=None, best_ask=None, bid_size=None, ask_size=None)

        bid = _plan(Side.BUY, state=state, prediction=make_prediction())

        self.assertFalse(bid.permitted)
        self.assertIs(bid.trigger, QuoteTrigger.MARKET_DATA_UNAVAILABLE)

    def test_retreat_beyond_price_is_rejected(self) -> None:
        state = market_state(best_bid=0.05, best_ask=0.06)
        config = maker_config(tick_size=0.01, max_back_ticks=10, minimum_edge_bps=0.0)

        bid = _plan(Side.BUY, state=state, config=config, prediction=make_prediction(), extra=6)

        self.assertFalse(bid.permitted)
        self.assertIs(bid.trigger, QuoteTrigger.SIDE_NOT_PERMITTED)

    def test_extra_retreat_ticks_are_clamped_and_reported(self) -> None:
        bid = _plan(Side.BUY, prediction=make_prediction(), extra=2)

        self.assertEqual(bid.retreat_ticks, 2)
        self.assertAlmostEqual(bid.price, 99.98, places=9)
        self.assertIn("inventory_retreat_ticks=2", bid.reasons)

    def test_missing_horizon_is_treated_as_against(self) -> None:
        prediction = make_prediction(horizon_ms=15_000)

        bid = _plan(Side.BUY, prediction=prediction)

        self.assertEqual(bid.retreat_ticks, 1)
        self.assertIn("predicted_move_against", bid.reasons)


if __name__ == "__main__":
    unittest.main()
