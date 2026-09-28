"""P0001.7 故障测试：库存偏置与风险上限下的行为（SC-6 / SC-11）。

对应提案指定的 `test_inventory_limit_behavior.py`。
"""

from __future__ import annotations

import unittest

from portfolio.types import Side
from risk.limits import RiskLimits
from risk.types import KillSwitchMode, RiskDecisionType
from strategy.maker import MakerPolicy, QuoteAction, QuoteMode
from tests.execution_support import ExecutionStack
from tests.strategy_support import make_record, maker_config, market_state
from tests.support import BASE_TS, SYMBOL, make_fill


class InventoryLimitBehaviorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExecutionStack.build(limits=RiskLimits(max_position_qty=10.0, max_open_order_exposure=500.0))
        self.policy = MakerPolicy(maker_config())
        self.state = market_state()

    def _seed(self, quantity: float) -> None:
        side = Side.BUY if quantity > 0 else Side.SELL
        self.stack.accounting.record_fill(make_fill("seed", side, 100.0, abs(quantity), exchange_ts=BASE_TS))

    def _decide(self, *, budget: float | None = 1_000.0, now_ms: int = BASE_TS, policy=None):
        resolved = policy if policy is not None else self.policy
        return resolved.decide(
            state=self.state,
            prediction=make_record(state=self.state),
            position=self.stack.accounting.position(SYMBOL),
            snapshot=self.stack.engine.snapshot(SYMBOL, now_ms=now_ms),
            existing_orders=self.stack.tracker.active(),
            remaining_risk_budget=budget,
            kill_switch=KillSwitchMode.NORMAL,
        )

    # ------------------------------------------------------------------ SC-6

    def test_sc6_long_inventory_shrinks_buy_and_grows_sell(self) -> None:
        flat = self._decide()
        self._seed(1.0)
        long_decision = self._decide()

        self.assertLess(long_decision.bid.proposal.quantity, flat.bid.proposal.quantity)
        self.assertGreaterEqual(long_decision.ask.proposal.quantity, flat.ask.proposal.quantity)
        self.assertTrue(long_decision.ask.proposal.reduce_only)
        self.assertFalse(long_decision.bid.proposal.reduce_only)

    def test_sc6_short_inventory_is_symmetric(self) -> None:
        flat = self._decide()
        self._seed(-1.0)
        short_decision = self._decide()

        self.assertLess(short_decision.ask.proposal.quantity, flat.ask.proposal.quantity)
        self.assertTrue(short_decision.bid.proposal.reduce_only)
        self.assertFalse(short_decision.ask.proposal.reduce_only)

    def test_long_inventory_retreats_the_buy_side(self) -> None:
        flat = self._decide()
        self._seed(5.0)
        long_decision = self._decide()

        self.assertLess(long_decision.bid.proposal.price, flat.bid.proposal.price)
        self.assertEqual(long_decision.ask.proposal.price, flat.ask.proposal.price)

    def test_inventory_change_triggers_a_size_drift_replace(self) -> None:
        decision = self._decide()
        for side_decision in decision.sides:
            self.stack.engine.submit(side_decision.proposal, now_ms=BASE_TS)
        self._seed(1.0)

        replacement = self._decide(now_ms=BASE_TS + 1)

        self.assertIs(replacement.bid.action, QuoteAction.REPLACE)
        self.assertIs(replacement.ask.action, QuoteAction.REPLACE)
        self.assertTrue(replacement.ask.proposal.reduce_only)

    def test_position_far_beyond_scale_still_respects_factor_bounds(self) -> None:
        self._seed(1_000.0)

        decision = self._decide()

        # normalized 被 clamp 到 1.0 → 买侧 factor = 1 - inventory_size_strength = 0.5
        self.assertAlmostEqual(decision.bid.proposal.quantity, 0.5)
        self.assertGreaterEqual(decision.bid.proposal.quantity, 0.2)  # >= size_factor_min
        # 卖侧 reduce-only：数量上界 = size_factor_max（1.5），且不超过 |position|
        self.assertAlmostEqual(decision.ask.proposal.quantity, 1.5)
        self.assertLessEqual(decision.ask.proposal.quantity, 1_000.0)

    # ------------------------------------------------------------------ SC-11

    def test_sc11_position_limit_rejects_the_policy_quote(self) -> None:
        stack = ExecutionStack.build(limits=RiskLimits(max_position_qty=1.0, max_open_order_exposure=500.0))
        stack.accounting.record_fill(make_fill("seed", Side.BUY, 100.0, 1.0, exchange_ts=BASE_TS))
        policy = MakerPolicy(maker_config())

        decision = policy.decide(
            state=self.state,
            prediction=make_record(state=self.state),
            position=stack.accounting.position(SYMBOL),
            snapshot=stack.engine.snapshot(SYMBOL, now_ms=BASE_TS),
            existing_orders=(),
            remaining_risk_budget=10_000.0,
            kill_switch=KillSwitchMode.NORMAL,
        )
        snapshot = stack.engine.snapshot(SYMBOL, now_ms=BASE_TS)

        self.assertTrue(decision.bid.proposal.reduce_only is False)
        rejected = stack.gate.evaluate(decision.bid.proposal, snapshot)
        self.assertIs(rejected.decision, RiskDecisionType.REJECT)
        self.assertEqual(rejected.reason_code.value, "POSITION_LIMIT")

        # 反手减仓侧仍然允许（RiskGate 的 reduce-only 语义）
        reducing = stack.gate.evaluate(decision.ask.proposal, snapshot)
        self.assertIs(reducing.decision, RiskDecisionType.ALLOW)

    def test_sc11_open_order_exposure_limit_blocks_the_second_quote(self) -> None:
        stack = ExecutionStack.build(limits=RiskLimits(max_position_qty=10.0, max_open_order_exposure=100.0))
        policy = MakerPolicy(maker_config())
        first = policy.decide(
            state=self.state,
            prediction=make_record(state=self.state),
            position=stack.accounting.position(SYMBOL),
            snapshot=stack.engine.snapshot(SYMBOL, now_ms=BASE_TS),
            existing_orders=(),
            remaining_risk_budget=100.0,
            kill_switch=KillSwitchMode.NORMAL,
        )
        stack.engine.submit(first.bid.proposal, now_ms=BASE_TS)  # 100.0 暴露

        rejected = stack.engine.submit(first.ask.proposal, now_ms=BASE_TS + 1)

        self.assertTrue(rejected.rejected)
        self.assertEqual(rejected.rejection.reason_code.value, "OPEN_ORDER_EXPOSURE_LIMIT")

    def test_exhausted_budget_keeps_the_reducing_side_only(self) -> None:
        self._seed(1.0)

        decision = self._decide(budget=0.0)

        self.assertIs(decision.mode, QuoteMode.ASK_ONLY)
        self.assertTrue(decision.ask.proposal.reduce_only)


if __name__ == "__main__":
    unittest.main()
