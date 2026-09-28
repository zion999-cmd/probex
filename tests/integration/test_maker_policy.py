"""P0001.7 集成测试：MarketState + Prediction + Position + RiskSnapshot → OrderProposal。

覆盖 SC-1（健康市场 + 有效预测生成 post-only 报价）、SC-3（市场不可用）、
SC-11（RiskGate 仍是最终权威）、SC-12（确定性）与 P0001.6.1 的交互（未知暴露）。
"""

from __future__ import annotations

import unittest

from execution.types import Order
from portfolio.types import Side
from risk.limits import RiskLimits
from risk.types import KillSwitchMode, RiskDecisionType
from strategy.maker import MakerPolicy, QuoteAction, QuoteMode, QuoteTrigger
from tests.execution_support import ExecutionStack
from tests.strategy_support import make_record, maker_config, market_state, with_exposure
from tests.support import BASE_TS, SYMBOL

BUDGET = 1_000.0


class MakerPolicyIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExecutionStack.build(limits=RiskLimits(max_position_qty=10.0, max_open_order_exposure=500.0))
        self.policy = MakerPolicy(maker_config())
        self.state = market_state()
        self.prediction = make_record(state=self.state)

    def _decide(self, *, state=None, prediction=None, budget=BUDGET, kill_switch=KillSwitchMode.NORMAL,
                now_ms=BASE_TS, orders: tuple[Order, ...] = (), snapshot=None):
        return self.policy.decide(
            state=state if state is not None else self.state,
            prediction=self.prediction if prediction is None else prediction,
            position=self.stack.accounting.position(SYMBOL),
            snapshot=snapshot if snapshot is not None else self.stack.engine.snapshot(SYMBOL, now_ms=now_ms),
            existing_orders=orders,
            remaining_risk_budget=budget,
            kill_switch=kill_switch,
        )

    # ------------------------------------------------------------------ SC-1

    def test_sc1_healthy_market_and_valid_prediction_produce_post_only_quotes(self) -> None:
        decision = self._decide()

        self.assertIs(decision.mode, QuoteMode.BOTH)
        self.assertIsNone(decision.blocked_by)
        for side_decision in decision.sides:
            self.assertIs(side_decision.action, QuoteAction.PLACE)
            self.assertTrue(side_decision.proposal.post_only)
            self.assertFalse(side_decision.proposal.reduce_only)
        self.assertLessEqual(decision.bid.proposal.price, self.state.price.best_bid)
        self.assertGreaterEqual(decision.ask.proposal.price, self.state.price.best_ask)

    def test_sc1_policy_quotes_survive_the_risk_gate(self) -> None:
        decision = self._decide()
        snapshot = self.stack.engine.snapshot(SYMBOL, now_ms=BASE_TS)

        for proposal in decision.quotes():
            with self.subTest(side=proposal.side.value):
                self.assertIs(self.stack.gate.evaluate(proposal, snapshot).decision, RiskDecisionType.ALLOW)

    # ------------------------------------------------------------------ SC-11

    def test_sc11_risk_gate_remains_the_authority(self) -> None:
        stack = ExecutionStack.build(limits=RiskLimits(max_position_qty=1.0, max_open_order_exposure=500.0))
        policy = MakerPolicy(maker_config(base_size=2.0))
        state = market_state()

        decision = policy.decide(
            state=state,
            prediction=make_record(state=state),
            position=stack.accounting.position(SYMBOL),
            snapshot=stack.engine.snapshot(SYMBOL, now_ms=BASE_TS),
            existing_orders=(),
            remaining_risk_budget=10_000.0,  # 策略层认为预算充足
            kill_switch=KillSwitchMode.NORMAL,
        )
        snapshot = stack.engine.snapshot(SYMBOL, now_ms=BASE_TS)
        buy = decision.bid.proposal

        self.assertAlmostEqual(buy.quantity, 2.0)  # 策略层没有自行套用 position limit
        rejected = stack.gate.evaluate(buy, snapshot)
        self.assertIs(rejected.decision, RiskDecisionType.REJECT)
        self.assertEqual(rejected.reason_code.value, "POSITION_LIMIT")
        self.assertTrue(stack.engine.submit(buy, now_ms=BASE_TS).rejected)

    # ------------------------------------------------------------------ SC-3

    def test_sc3_unhealthy_market_quotes_nothing_and_cancels_existing(self) -> None:
        quote = self.stack.submit_order(self.stack.proposal(quantity=1.0, price=100.0), now_ms=BASE_TS)
        unhealthy = market_state(tradeable=False)

        decision = self._decide(state=unhealthy, orders=(quote,))

        self.assertIs(decision.mode, QuoteMode.NONE)
        self.assertIs(decision.blocked_by, QuoteTrigger.MARKET_UNHEALTHY)
        self.assertIs(decision.bid.action, QuoteAction.CANCEL)
        self.assertIs(decision.ask.action, QuoteAction.NONE)

    def test_missing_book_sides_are_not_quoted(self) -> None:
        decision = self._decide(state=market_state(best_bid=None, best_ask=None, bid_size=None, ask_size=None))

        self.assertIs(decision.mode, QuoteMode.NONE)
        self.assertIs(decision.bid.trigger, QuoteTrigger.MARKET_DATA_UNAVAILABLE)
        self.assertIs(decision.ask.trigger, QuoteTrigger.MARKET_DATA_UNAVAILABLE)

    # ------------------------------------------------------------------ Kill switch / 未知暴露

    def test_halt_all_cancels_everything(self) -> None:
        quote = self.stack.submit_order(self.stack.proposal(quantity=1.0, price=100.0), now_ms=BASE_TS)

        decision = self._decide(kill_switch=KillSwitchMode.HALT_ALL, orders=(quote,))

        self.assertIs(decision.blocked_by, QuoteTrigger.KILL_SWITCH)
        self.assertIs(decision.bid.action, QuoteAction.CANCEL)
        self.assertEqual(decision.quotes(), ())

    def test_reduce_only_kill_switch_allows_reducing_quotes_only(self) -> None:
        self.stack.accounting.record_fill(self._seed_long())

        decision = self._decide(kill_switch=KillSwitchMode.REDUCE_ONLY)

        self.assertIs(decision.mode, QuoteMode.ASK_ONLY)
        self.assertTrue(decision.ask.proposal.reduce_only)
        self.assertIs(decision.bid.action, QuoteAction.NONE)
        self.assertIs(decision.bid.trigger, QuoteTrigger.KILL_SWITCH)

    def test_unknown_exposure_blocks_new_exposure(self) -> None:
        snapshot = with_exposure(self.stack.engine.snapshot(SYMBOL, now_ms=BASE_TS), unresolved_order_count=1)

        decision = self._decide(snapshot=snapshot)

        self.assertIs(decision.mode, QuoteMode.NONE)
        self.assertIs(decision.blocked_by, QuoteTrigger.UNKNOWN_EXPOSURE)

    # ------------------------------------------------------------------ 库存 / reduce-only

    def test_long_position_marks_only_the_sell_side_reduce_only(self) -> None:
        self.stack.accounting.record_fill(self._seed_long())

        decision = self._decide()

        self.assertTrue(decision.ask.proposal.reduce_only)
        self.assertFalse(decision.bid.proposal.reduce_only)
        # 买侧：1.0 × 0.5（库存缩量）；卖侧：1.0 × 1.5 但被 |position| = 1.0 截断
        self.assertAlmostEqual(decision.bid.proposal.quantity, 0.5)
        self.assertAlmostEqual(decision.ask.proposal.quantity, 1.0)
        self.assertLess(decision.bid.proposal.quantity, decision.ask.proposal.quantity)

    def test_reduce_only_size_never_exceeds_the_position(self) -> None:
        self.stack.accounting.record_fill(self._seed_long(quantity=0.1, price=100.0))

        decision = self._decide()

        self.assertTrue(decision.ask.proposal.reduce_only)
        self.assertLessEqual(decision.ask.proposal.quantity, 0.1 + 1e-9)

    # ------------------------------------------------------------------ SC-12

    def test_sc12_same_inputs_produce_identical_decisions(self) -> None:
        quote = self.stack.submit_order(self.stack.proposal(quantity=1.0, price=100.0), now_ms=BASE_TS)
        orders = self.stack.tracker.orders

        first = self._decide(orders=orders)
        second = self._decide(orders=orders)

        self.assertEqual(first, second)
        self.assertEqual(tuple(order.client_order_id for order in orders), ("probex-s1-000001",))
        self.assertIs(quote.status.is_active, True)

    def test_sc12_fresh_policy_instance_matches(self) -> None:
        first = self._decide()
        second = MakerPolicy(maker_config()).decide(
            state=self.state,
            prediction=self.prediction,
            position=self.stack.accounting.position(SYMBOL),
            snapshot=self.stack.engine.snapshot(SYMBOL, now_ms=BASE_TS),
            existing_orders=(),
            remaining_risk_budget=BUDGET,
            kill_switch=KillSwitchMode.NORMAL,
        )

        self.assertEqual(first, second)

    # ------------------------------------------------------------------ 输入契约

    def test_mismatched_position_and_snapshot_are_rejected(self) -> None:
        from tests.strategy_support import position_for

        with self.assertRaises(ValueError):
            self.policy.decide(
                state=self.state,
                prediction=self.prediction,
                position=position_for(1.0),
                snapshot=self.stack.engine.snapshot(SYMBOL, now_ms=BASE_TS),
                existing_orders=(),
                remaining_risk_budget=BUDGET,
                kill_switch=KillSwitchMode.NORMAL,
            )

    def test_negative_budget_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._decide(budget=-1.0)

    def test_foreign_symbol_order_is_rejected(self) -> None:
        foreign = ExecutionStack.build(symbol="ETHUSDT")
        order = foreign.submit_order(foreign.proposal(quantity=1.0, price=100.0), now_ms=BASE_TS)

        with self.assertRaises(ValueError):
            self._decide(orders=(order,))

    def _seed_long(self, *, quantity: float = 1.0, price: float = 100.0):
        from tests.support import make_fill

        return make_fill("seed", Side.BUY, price, quantity, exchange_ts=BASE_TS)


if __name__ == "__main__":
    unittest.main()
