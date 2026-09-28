"""P0001.7 故障测试：adverse selection 与 cost floor 的阻止 / 后退行为（SC-4 / SC-5 / SC-8）。

对应提案指定的 `test_adverse_selection_block.py`。
"""

from __future__ import annotations

import unittest

from portfolio.types import Side
from risk.limits import RiskLimits
from risk.types import KillSwitchMode
from strategy.maker import MakerPolicy, QuoteAction, QuoteMode, QuoteTrigger
from tests.execution_support import ExecutionStack
from tests.strategy_support import make_prediction, make_record, maker_config, market_state
from tests.support import BASE_TS, SYMBOL

BUDGET = 1_000.0


class AdverseSelectionBlockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExecutionStack.build(limits=RiskLimits(max_position_qty=10.0, max_open_order_exposure=500.0))

    def _decide(self, *, policy=None, state=None, prediction=None, now_ms: int = BASE_TS, budget: float | None = BUDGET):
        resolved_state = state if state is not None else market_state()
        resolved_policy = policy if policy is not None else MakerPolicy(maker_config())
        return resolved_policy.decide(
            state=resolved_state,
            prediction=make_record(state=resolved_state, prediction=prediction),
            position=self.stack.accounting.position(SYMBOL),
            snapshot=self.stack.engine.snapshot(SYMBOL, now_ms=now_ms),
            existing_orders=self.stack.tracker.active(),
            remaining_risk_budget=budget,
            kill_switch=KillSwitchMode.NORMAL,
        )

    # ------------------------------------------------------------------ SC-4

    def test_sc4_blocked_side_is_not_quoted(self) -> None:
        prediction = make_prediction(buy_adverse_selection=0.9)

        decision = self._decide(prediction=prediction)

        self.assertIs(decision.mode, QuoteMode.ASK_ONLY)  # 单边报价是允许的
        self.assertIs(decision.bid.action, QuoteAction.NONE)
        self.assertIs(decision.bid.trigger, QuoteTrigger.ADVERSE_SELECTION)
        self.assertIs(decision.ask.action, QuoteAction.PLACE)
        self.assertIs(decision.ask.trigger, QuoteTrigger.INITIAL_QUOTE)

    def test_sc4_blocked_side_cancels_its_resting_quote(self) -> None:
        first = self._decide()
        for side_decision in first.sides:
            self.stack.engine.submit(side_decision.proposal, now_ms=BASE_TS)

        decision = self._decide(prediction=make_prediction(buy_adverse_selection=0.9))

        self.assertIs(decision.bid.action, QuoteAction.CANCEL)
        self.assertIs(decision.bid.trigger, QuoteTrigger.ADVERSE_SELECTION)
        self.assertIs(decision.ask.action, QuoteAction.KEEP)

    def test_retreat_threshold_moves_the_quote_back(self) -> None:
        baseline = self._decide(prediction=make_prediction())
        retreated = self._decide(prediction=make_prediction(buy_adverse_selection=0.35))

        self.assertLess(retreated.bid.proposal.price, baseline.bid.proposal.price)
        self.assertEqual(retreated.ask.proposal.price, baseline.ask.proposal.price)
        self.assertLessEqual(retreated.bid.proposal.price, retreated.bid.proposal.price)

    def test_retreat_never_crosses_the_book(self) -> None:
        decision = self._decide(
            state=market_state(),
            prediction=make_prediction(buy_adverse_selection=0.35, sell_adverse_selection=0.35),
        )

        self.assertLessEqual(decision.bid.proposal.price, market_state().price.best_bid)
        self.assertGreaterEqual(decision.ask.proposal.price, market_state().price.best_ask)

    # ------------------------------------------------------------------ SC-5

    def test_sc5_high_fill_probability_does_not_enlarge_the_quote(self) -> None:
        low = self._decide(prediction=make_prediction(buy_fill_probability=0.05, sell_fill_probability=0.05))
        high = self._decide(prediction=make_prediction(buy_fill_probability=0.99, sell_fill_probability=0.99))

        self.assertEqual(low.quotes(), high.quotes())

    def test_sc5_high_fill_probability_with_high_adverse_selection_still_blocks(self) -> None:
        prediction = make_prediction(
            buy_fill_probability=0.99, buy_adverse_selection=0.95, sell_fill_probability=0.99
        )

        decision = self._decide(prediction=prediction)

        self.assertIs(decision.bid.action, QuoteAction.NONE)
        self.assertIs(decision.bid.trigger, QuoteTrigger.ADVERSE_SELECTION)

    # ------------------------------------------------------------------ SC-8

    def test_sc8_narrow_spread_and_high_cost_floor_block_both_sides(self) -> None:
        state = market_state(best_bid=100.0, best_ask=100.01)
        policy = MakerPolicy(maker_config(minimum_edge_bps=200.0, max_back_ticks=1))

        decision = self._decide(policy=policy, state=state)

        self.assertIs(decision.mode, QuoteMode.NONE)
        for side_decision in decision.sides:
            self.assertIs(side_decision.trigger, QuoteTrigger.COST_FLOOR_UNMET)
            self.assertIsNone(side_decision.proposal)

    def test_sc8_cost_floor_is_satisfied_by_retreating(self) -> None:
        state = market_state(best_bid=100.0, best_ask=100.01)
        policy = MakerPolicy(maker_config(minimum_edge_bps=1.0))

        decision = self._decide(policy=policy, state=state)

        self.assertIs(decision.mode, QuoteMode.BOTH)
        for side_decision in decision.sides:
            self.assertIsNotNone(side_decision.proposal)

    def test_sc8_reduce_only_is_exempt_from_the_cost_floor(self) -> None:
        from tests.support import make_fill

        self.stack.accounting.record_fill(make_fill("seed", Side.BUY, 100.0, 1.0, exchange_ts=BASE_TS))
        state = market_state(best_bid=100.0, best_ask=100.01)
        policy = MakerPolicy(maker_config(minimum_edge_bps=200.0, max_back_ticks=1))

        decision = self._decide(policy=policy, state=state)

        self.assertIs(decision.mode, QuoteMode.ASK_ONLY)
        self.assertTrue(decision.ask.proposal.reduce_only)
        self.assertGreaterEqual(decision.ask.proposal.price, state.price.best_ask)

    def test_missing_budget_blocks_new_exposure_only(self) -> None:
        from tests.support import make_fill

        self.stack.accounting.record_fill(make_fill("seed", Side.BUY, 100.0, 1.0, exchange_ts=BASE_TS))

        decision = self._decide(budget=None)

        self.assertIs(decision.mode, QuoteMode.ASK_ONLY)
        self.assertIs(decision.bid.trigger, QuoteTrigger.RISK_BUDGET_UNKNOWN)
        self.assertTrue(decision.ask.proposal.reduce_only)


if __name__ == "__main__":
    unittest.main()
