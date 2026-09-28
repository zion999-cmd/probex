"""P0001.7.1 故障测试：prediction 中断期间的 reduce-only 连续性（SC-1 – SC-7）。

场景：已有仓位 + Jev 暂时故障 / prediction 过期 / 无存量退出挂单。
契约：**禁止增加 exposure，但允许安全的 reduce-only Maker 报价**。
"""

from __future__ import annotations

import unittest

from portfolio.types import Side
from risk.limits import RiskLimits
from risk.types import KillSwitchMode
from strategy.maker import MakerPolicy, QuoteAction, QuoteMode, QuoteTrigger, fresh_prediction
from tests.execution_support import ExecutionStack
from tests.strategy_support import make_record, maker_config, market_state
from tests.support import BASE_TS, SYMBOL, make_fill

BUDGET = 1_000.0
EXPIRED_TTL_MS = 1_000


class PredictionOutageReduceOnlyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExecutionStack.build(limits=RiskLimits(max_position_qty=10.0, max_open_order_exposure=500.0))
        self.policy = MakerPolicy(maker_config())
        self.state = market_state()

    def _seed(self, quantity: float) -> None:
        side = Side.BUY if quantity > 0 else Side.SELL
        self.stack.accounting.record_fill(make_fill("seed", side, 100.0, abs(quantity), exchange_ts=BASE_TS))

    def _decide(self, *, prediction, now_ms: int = BASE_TS, budget: float | None = BUDGET,
                kill_switch=KillSwitchMode.NORMAL, state=None):
        return self.policy.decide(
            state=self.state if state is None else state,
            prediction=prediction,
            position=self.stack.accounting.position(SYMBOL),
            snapshot=self.stack.engine.snapshot(SYMBOL, now_ms=now_ms),
            existing_orders=self.stack.tracker.active(),
            remaining_risk_budget=budget,
            kill_switch=kill_switch,
        )

    def _outage_decision(self, **kwargs):
        """持仓场景下「无可用 prediction」的一轮决策。"""
        expired = make_record(state=self.state, now_ms=BASE_TS, ttl_ms=EXPIRED_TTL_MS)
        return self._decide(prediction=expired, now_ms=BASE_TS + 10 * EXPIRED_TTL_MS, **kwargs)

    # ------------------------------------------------------------------ SC-1 / SC-2

    def test_sc1_long_position_without_prediction_can_quote_reduce_only_sell(self) -> None:
        self._seed(1.0)

        decision = self._outage_decision()

        self.assertIs(decision.blocked_by, QuoteTrigger.PREDICTION_STALE)
        self.assertIs(decision.mode, QuoteMode.ASK_ONLY)
        self.assertIs(decision.ask.action, QuoteAction.PLACE)
        proposal = decision.ask.proposal
        self.assertTrue(proposal.reduce_only)
        self.assertTrue(proposal.post_only)
        self.assertEqual(proposal.side, Side.SELL)
        self.assertLessEqual(proposal.quantity, 1.0 + 1e-9)  # 不超过持仓
        self.assertGreaterEqual(proposal.price, self.state.price.best_ask)  # 不穿盘口

    def test_sc2_short_position_without_prediction_can_quote_reduce_only_buy(self) -> None:
        self._seed(-1.0)

        decision = self._outage_decision()

        self.assertIs(decision.mode, QuoteMode.BID_ONLY)
        self.assertIs(decision.bid.action, QuoteAction.PLACE)
        proposal = decision.bid.proposal
        self.assertTrue(proposal.reduce_only)
        self.assertEqual(proposal.side, Side.BUY)
        self.assertLessEqual(proposal.quantity, 1.0 + 1e-9)
        self.assertLessEqual(proposal.price, self.state.price.best_bid)

    def test_outage_reduce_only_quote_passes_the_risk_gate(self) -> None:
        self._seed(1.0)

        decision = self._outage_decision()
        snapshot = self.stack.engine.snapshot(SYMBOL, now_ms=BASE_TS + 10 * EXPIRED_TTL_MS)

        self.assertTrue(self.stack.gate.evaluate(decision.ask.proposal, snapshot).allowed)
        self.assertFalse(self.stack.engine.submit(decision.ask.proposal, now_ms=BASE_TS).rejected)

    def test_outage_without_a_position_quotes_nothing(self) -> None:
        decision = self._outage_decision()

        self.assertIs(decision.mode, QuoteMode.NONE)
        self.assertEqual(decision.quotes(), ())

    # ------------------------------------------------------------------ SC-3

    def test_sc3_no_increasing_quote_is_generated_during_the_outage(self) -> None:
        self._seed(1.0)

        decision = self._outage_decision()

        self.assertIs(decision.bid.action, QuoteAction.NONE)
        self.assertIs(decision.bid.trigger, QuoteTrigger.PREDICTION_STALE)
        self.assertEqual([proposal.reduce_only for proposal in decision.quotes()], [True])

    def test_sc3_increasing_side_is_dropped_but_reduce_only_side_keeps_quoting(self) -> None:
        self._seed(1.0)
        first = self._decide(prediction=make_record(state=self.state))
        for side_decision in first.sides:
            self.stack.engine.submit(side_decision.proposal, now_ms=BASE_TS)

        decision = self._outage_decision()

        self.assertIs(decision.bid.action, QuoteAction.CANCEL)
        self.assertIsNot(decision.ask.action, QuoteAction.NONE)

    # ------------------------------------------------------------------ SC-4 / SC-5

    def test_sc4_stale_prediction_cancels_the_increasing_buy_quote(self) -> None:
        self._seed(1.0)
        first = self._decide(prediction=make_record(state=self.state))
        bid_order = self.stack.engine.submit(first.bid.proposal, now_ms=BASE_TS).order

        decision = self._outage_decision()

        self.assertIs(decision.bid.action, QuoteAction.CANCEL)
        self.assertEqual(decision.bid.client_order_id, bid_order.client_order_id)
        self.assertIs(decision.bid.trigger, QuoteTrigger.PREDICTION_STALE)

    def test_sc5_existing_reduce_only_quote_is_not_canceled(self) -> None:
        self._seed(1.0)
        first = self._decide(prediction=make_record(state=self.state))
        ask_order = self.stack.engine.submit(first.ask.proposal, now_ms=BASE_TS).order

        decision = self._outage_decision()

        self.assertIs(decision.ask.client_order_id, ask_order.client_order_id)
        self.assertIn(decision.ask.action, (QuoteAction.KEEP, QuoteAction.REPLACE))

    def test_sc5_missing_prediction_also_keeps_reduce_only_continuity(self) -> None:
        self._seed(-1.0)

        decision = self._decide(prediction=None)

        self.assertIs(decision.mode, QuoteMode.BID_ONLY)
        self.assertTrue(decision.bid.proposal.reduce_only)

    # ------------------------------------------------------------------ SC-6

    def test_sc6_unhealthy_market_still_blocks_reduce_only_quotes(self) -> None:
        self._seed(1.0)

        decision = self._outage_decision(state=market_state(tradeable=False))

        self.assertIs(decision.mode, QuoteMode.NONE)
        self.assertIs(decision.blocked_by, QuoteTrigger.MARKET_UNHEALTHY)

    def test_sc6_halt_all_still_blocks_and_cancels_everything(self) -> None:
        self._seed(1.0)
        first = self._decide(prediction=make_record(state=self.state))
        ask_order = self.stack.engine.submit(first.ask.proposal, now_ms=BASE_TS).order

        decision = self._outage_decision(kill_switch=KillSwitchMode.HALT_ALL)

        self.assertIs(decision.blocked_by, QuoteTrigger.KILL_SWITCH)
        self.assertIs(decision.mode, QuoteMode.NONE)
        self.assertIs(decision.ask.action, QuoteAction.CANCEL)
        self.assertEqual(decision.ask.client_order_id, ask_order.client_order_id)

    def test_sc6_missing_book_blocks_reduce_only_quotes(self) -> None:
        self._seed(1.0)

        decision = self._outage_decision(state=market_state(best_bid=None, best_ask=None, bid_size=None, ask_size=None))

        self.assertIs(decision.mode, QuoteMode.NONE)
        self.assertIs(decision.ask.trigger, QuoteTrigger.MARKET_DATA_UNAVAILABLE)

    def test_sc6_reduce_only_kill_switch_still_allows_reduce_only(self) -> None:
        self._seed(1.0)

        decision = self._decide(prediction=None, kill_switch=KillSwitchMode.REDUCE_ONLY)

        self.assertIs(decision.mode, QuoteMode.ASK_ONLY)
        self.assertTrue(decision.ask.proposal.reduce_only)

    # ------------------------------------------------------------------ SC-7

    def test_sc7_risk_gate_remains_the_final_authority(self) -> None:
        self._seed(1.0)
        strict_gate_stack = ExecutionStack.build(
            limits=RiskLimits(max_position_qty=10.0, kill_switch_mode=KillSwitchMode.HALT_ALL)
        )
        strict_gate_stack.accounting.record_fill(make_fill("seed", Side.BUY, 100.0, 1.0, exchange_ts=BASE_TS))
        decision = self._outage_decision()

        rejected = strict_gate_stack.gate.evaluate(
            decision.ask.proposal, strict_gate_stack.engine.snapshot(SYMBOL, now_ms=BASE_TS)
        )

        self.assertTrue(rejected.rejected)
        self.assertEqual(rejected.reason_code.value, "KILL_SWITCH")
        self.assertTrue(strict_gate_stack.engine.submit(decision.ask.proposal, now_ms=BASE_TS).rejected)

    def test_expired_prediction_is_not_used_for_direction(self) -> None:
        expired = make_record(state=self.state, now_ms=BASE_TS, ttl_ms=EXPIRED_TTL_MS)

        self.assertIsNone(
            fresh_prediction(prediction=expired, config=self.policy.config, now_ms=BASE_TS + 10 * EXPIRED_TTL_MS)
        )
        self.assertIs(
            fresh_prediction(prediction=expired, config=self.policy.config, now_ms=BASE_TS + EXPIRED_TTL_MS),
            expired,
        )

    def test_outage_reduce_only_decisions_are_deterministic(self) -> None:
        self._seed(1.0)

        first = self._outage_decision()
        second = self._outage_decision()

        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
