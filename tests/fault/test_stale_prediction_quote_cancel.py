"""P0001.7 故障测试：prediction stale 时不得新增报价，并撤掉增加暴露的报价（SC-2）。

对应提案指定的 `test_stale_prediction_quote_cancel.py`。
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


class StalePredictionQuoteCancelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExecutionStack.build(limits=RiskLimits(max_position_qty=10.0, max_open_order_exposure=500.0))
        self.policy = MakerPolicy(maker_config())
        self.state = market_state()

    def _decide(self, *, prediction, now_ms: int = BASE_TS):
        return self.policy.decide(
            state=self.state,
            prediction=prediction,
            position=self.stack.accounting.position(SYMBOL),
            snapshot=self.stack.engine.snapshot(SYMBOL, now_ms=now_ms),
            existing_orders=self.stack.tracker.active(),
            remaining_risk_budget=BUDGET,
            kill_switch=KillSwitchMode.NORMAL,
        )

    def _rest_quotes(self) -> tuple[str, str]:
        decision = self._decide(prediction=make_record(state=self.state))
        for side_decision in decision.sides:
            self.stack.engine.submit(side_decision.proposal, now_ms=BASE_TS)
        return tuple(order.client_order_id for order in self.stack.tracker.active())  # type: ignore[return-value]

    def test_sc2_expired_prediction_blocks_new_quotes_and_cancels_resting_ones(self) -> None:
        self._rest_quotes()

        expired = make_record(state=self.state, now_ms=BASE_TS, ttl_ms=1_000)
        decision = self._decide(prediction=expired, now_ms=BASE_TS + 10_000)

        self.assertIs(decision.blocked_by, QuoteTrigger.PREDICTION_STALE)
        self.assertIs(decision.mode, QuoteMode.NONE)
        self.assertEqual(decision.quotes(), ())
        for side_decision in decision.sides:
            self.assertIs(side_decision.action, QuoteAction.CANCEL)
            self.assertIs(side_decision.trigger, QuoteTrigger.PREDICTION_STALE)

    def test_sc2_missing_prediction_blocks_new_quotes(self) -> None:
        decision = self._decide(prediction=None)

        self.assertIs(decision.mode, QuoteMode.NONE)
        self.assertIs(decision.blocked_by, QuoteTrigger.PREDICTION_STALE)
        self.assertEqual(decision.bid.action, QuoteAction.NONE)

    def test_sc2_missing_horizon_blocks_new_quotes(self) -> None:
        record = make_record(state=self.state, prediction=make_prediction(horizon_ms=60_000))

        decision = self._decide(prediction=record)

        self.assertIs(decision.blocked_by, QuoteTrigger.PREDICTION_STALE)
        self.assertIn("horizon", decision.detail)

    def test_expiry_boundary_is_inclusive(self) -> None:
        record = make_record(state=self.state, now_ms=BASE_TS, ttl_ms=1_000)

        allowed = self._decide(prediction=record, now_ms=BASE_TS + 1_000)
        blocked = self._decide(prediction=record, now_ms=BASE_TS + 1_001)

        self.assertIsNone(allowed.blocked_by)
        self.assertIs(blocked.blocked_by, QuoteTrigger.PREDICTION_STALE)

    def test_sc5_reduce_only_quote_is_never_canceled_by_a_stale_prediction(self) -> None:
        """P0001.7.1 / SC-5：stale prediction 下合法 reduce-only 报价可 KEEP 或 REPLACE，但不得被撤掉。"""
        from tests.support import make_fill

        self.stack.accounting.record_fill(make_fill("seed", Side.BUY, 100.0, 1.0, exchange_ts=BASE_TS))
        self._rest_quotes()
        ask_id = next(order.client_order_id for order in self.stack.tracker.active() if order.side is Side.SELL)

        expired = make_record(state=self.state, now_ms=BASE_TS, ttl_ms=1_000)
        decision = self._decide(prediction=expired, now_ms=BASE_TS + 10_000)

        ask_decision = next(side_decision for side_decision in decision.sides if side_decision.client_order_id == ask_id)
        self.assertIn(ask_decision.action, (QuoteAction.KEEP, QuoteAction.REPLACE))
        self.assertNotIn(ask_decision.action, (QuoteAction.CANCEL, QuoteAction.NONE))
        self.assertTrue((ask_decision.proposal or self.stack.order(ask_id)).reduce_only)

    def test_sc4_increasing_resting_quote_is_canceled_on_stale_prediction(self) -> None:
        """SC-4：prediction 过期时，增加 exposure 的旧报价必须被撤掉。"""
        from tests.support import make_fill

        self.stack.accounting.record_fill(make_fill("seed", Side.BUY, 100.0, 1.0, exchange_ts=BASE_TS))
        self._rest_quotes()
        bid_id = next(order.client_order_id for order in self.stack.tracker.active() if order.side is Side.BUY)

        expired = make_record(state=self.state, now_ms=BASE_TS, ttl_ms=1_000)
        decision = self._decide(prediction=expired, now_ms=BASE_TS + 10_000)

        self.assertIs(decision.bid.action, QuoteAction.CANCEL)
        self.assertIs(decision.bid.trigger, QuoteTrigger.PREDICTION_STALE)
        self.assertEqual(decision.bid.client_order_id, bid_id)

    def test_regenerated_prediction_resumes_quoting(self) -> None:
        self._rest_quotes()
        stale = self._decide(prediction=None)
        self.assertIs(stale.mode, QuoteMode.NONE)

        fresh = self._decide(prediction=make_record(state=self.state, now_ms=BASE_TS))

        self.assertIs(fresh.mode, QuoteMode.BOTH)  # 旧单仍在（撤单请求），因此仍是 KEEP/REPLACE 语义
        self.assertIsNone(fresh.blocked_by)


if __name__ == "__main__":
    unittest.main()
