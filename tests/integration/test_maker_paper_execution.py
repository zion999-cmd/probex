"""P0001.7 集成测试：策略决策 → RiskGate → ExecutionEngine → PaperBroker（SC-10）。

验证策略层与 P0001.6 执行层的接缝：

- 策略只产出决策，真正的撤单 / 下单由调用方（本测试中的 driver）执行；
- `REPLACE` 必须走 P0001.6 的 cancel-before-replace：旧单进入确认终态后才下新单；
- 成交进入 Accounting 后，下一轮决策会随库存变化而改变（SIZE_DRIFT / reduce-only）。
"""

from __future__ import annotations

import unittest

from execution.types import Order
from portfolio.types import Side
from risk.limits import RiskLimits
from risk.types import KillSwitchMode
from strategy.maker import MakerPolicy, QuoteAction
from tests.execution_support import ExecutionStack
from tests.strategy_support import make_record, make_prediction, maker_config, market_state
from tests.support import BASE_TS, SYMBOL

BUDGET = 1_000.0


def drive(stack: ExecutionStack, decision, *, now_ms: int) -> None:
    """按决策驱动执行层：CANCEL → engine.cancel；PLACE → engine.submit；REPLACE → manager.replace。"""
    for side_decision in decision.sides:
        if side_decision.action is QuoteAction.CANCEL:
            stack.engine.cancel(side_decision.client_order_id, now_ms=now_ms)
        elif side_decision.action is QuoteAction.PLACE:
            stack.engine.submit(side_decision.proposal, now_ms=now_ms)
        elif side_decision.action is QuoteAction.REPLACE:
            stack.manager.replace(side_decision.client_order_id, side_decision.proposal, timestamp=now_ms)


class MakerPaperExecutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExecutionStack.build(limits=RiskLimits(max_position_qty=10.0, max_open_order_exposure=500.0))
        self.policy = MakerPolicy(maker_config())
        self.state = market_state()

    def _decide(self, *, state=None, now_ms: int = BASE_TS, prediction=None):
        return self.policy.decide(
            state=self.state if state is None else state,
            prediction=make_record(state=self.state) if prediction is None else prediction,
            position=self.stack.accounting.position(SYMBOL),
            snapshot=self.stack.engine.snapshot(SYMBOL, now_ms=now_ms),
            existing_orders=self.stack.tracker.active(),
            remaining_risk_budget=BUDGET,
            kill_switch=KillSwitchMode.NORMAL,
        )

    def test_quotes_are_placed_and_filled_into_accounting(self) -> None:
        decision = self._decide()
        drive(self.stack, decision, now_ms=BASE_TS)

        orders = self.stack.tracker.active()
        self.assertEqual(len(orders), 2)

        bid_order = next(order for order in orders if order.side is Side.BUY)
        fill = self.stack.fill(bid_order, quantity=0.25, price=bid_order.price, timestamp=BASE_TS + 1)

        self.assertEqual([update.outcome.value for update in fill.updates], ["fill_applied"])
        position = self.stack.accounting.position(SYMBOL)
        self.assertAlmostEqual(position.qty, 0.25)
        self.assertAlmostEqual(position.avg_entry_price, bid_order.price)

    def test_keep_when_nothing_changed(self) -> None:
        drive(self.stack, self._decide(), now_ms=BASE_TS)

        second = self._decide(now_ms=BASE_TS + 100)

        self.assertEqual([side.action.value for side in second.sides], ["keep", "keep"])
        self.assertEqual(len(self.stack.tracker.active()), 2)

    def test_sc10_replace_goes_through_cancel_before_replace(self) -> None:
        drive(self.stack, self._decide(), now_ms=BASE_TS)
        bid_order = next(order for order in self.stack.tracker.active() if order.side is Side.BUY)
        self.stack.broker.defer_cancel_ack(bid_order.client_order_id)

        # 盘口上移 5 ticks → 买侧报价偏离 ≥ price_move_ticks_replace → REPLACE
        moved = market_state(best_bid=100.05, best_ask=101.05)
        decision = self._decide(state=moved, now_ms=BASE_TS + 200)
        bid_decision = decision.bid

        self.assertIs(bid_decision.action, QuoteAction.REPLACE)
        self.assertEqual(bid_decision.client_order_id, bid_order.client_order_id)
        self.assertIn(bid_order.client_order_id, decision.cancels())

        # 旧单尚未确认终态 → manager.replace 只请求撤单，不下新单
        outcome = self.stack.manager.replace(bid_order.client_order_id, bid_decision.proposal, timestamp=BASE_TS + 201)
        self.assertTrue(outcome.requested_cancel)
        self.assertFalse(outcome.replaced)
        self.assertIs(self.stack.tracker.require_order(bid_order.client_order_id).status.value, "PENDING_CANCEL")
        self.assertEqual(len(self.stack.broker.open_orders()), 2)  # 没有新单

        # 撤单确认后再次 replace → 才真正下新单
        self.stack.broker.cancel_ack(bid_order.client_order_id, timestamp=BASE_TS + 202)
        self.stack.poll(now_ms=BASE_TS + 202)
        outcome = self.stack.manager.replace(bid_order.client_order_id, bid_decision.proposal, timestamp=BASE_TS + 203)

        self.assertTrue(outcome.replaced)
        self.assertEqual(outcome.order.price, bid_decision.proposal.price)
        self.assertIs(self.stack.tracker.require_order(bid_order.client_order_id).status.value, "CANCELED")

    def test_lifecycle_cancels_the_increasing_side_after_a_fill_changes_inventory(self) -> None:
        drive(self.stack, self._decide(), now_ms=BASE_TS)
        bid_order = next(order for order in self.stack.tracker.active() if order.side is Side.BUY)
        self.stack.fill(bid_order, quantity=0.5, price=bid_order.price, timestamp=BASE_TS + 1)

        decision = self._decide(now_ms=BASE_TS + 2)

        # 仓位变多 → 卖侧变为 reduce-only（reduce_only 语义变化 → REPLACE），买侧缩量（SIZE_DRIFT）
        self.assertIs(decision.ask.action, QuoteAction.REPLACE)
        self.assertTrue(decision.ask.proposal.reduce_only)
        self.assertIs(decision.bid.action, QuoteAction.REPLACE)
        self.assertIs(decision.bid.trigger.value, "size_drift")
        self.assertAlmostEqual(decision.bid.proposal.quantity, 0.75)
        self.assertLess(decision.bid.proposal.quantity, bid_order.quantity)

    def test_fully_filled_side_is_re_placed(self) -> None:
        drive(self.stack, self._decide(), now_ms=BASE_TS)
        bid_order = next(order for order in self.stack.tracker.active() if order.side is Side.BUY)
        self.stack.fill(bid_order, quantity=1.0, price=bid_order.price, timestamp=BASE_TS + 1)

        decision = self._decide(now_ms=BASE_TS + 2)

        self.assertIs(decision.bid.action, QuoteAction.PLACE)  # 旧单已终态 → 全新报价
        self.assertIsNone(decision.bid.client_order_id)

    def test_unhealthy_market_cancels_resting_quotes(self) -> None:
        drive(self.stack, self._decide(), now_ms=BASE_TS)

        decision = self._decide(state=market_state(tradeable=False), now_ms=BASE_TS + 100)
        drive(self.stack, decision, now_ms=BASE_TS + 101)

        active = self.stack.tracker.active()
        self.assertEqual(active, ())
        self.assertTrue(all(order.status.value == "CANCELED" for order in self.stack.tracker.orders))

    def test_reduce_only_exit_quote_survives_a_prediction_outage(self) -> None:
        """P0001.7.1：prediction 中断时必须仍有一个 reduce-only 出口报价，且不得再有增加暴露的报价。"""
        from tests.support import make_fill

        self.stack.accounting.record_fill(make_fill("seed", Side.BUY, 100.0, 1.0, exchange_ts=BASE_TS))
        drive(self.stack, self._decide(), now_ms=BASE_TS)
        bid_order = next(order for order in self.stack.tracker.active() if order.side is Side.BUY)

        stale = make_record(state=self.state, now_ms=BASE_TS, ttl_ms=1_000)  # 在 now 之后过期
        decision = self._decide(now_ms=BASE_TS + 5_000, prediction=stale)

        self.assertIs(decision.bid.action, QuoteAction.CANCEL)
        self.assertIn(decision.ask.action, (QuoteAction.KEEP, QuoteAction.REPLACE))
        self.assertTrue(decision.ask.proposal.reduce_only or decision.ask.action is QuoteAction.KEEP)

        # 真实驱动：REPLACE 需要两轮（撤单确认后才下新单），与 P0001.6 的 cancel-before-replace 一致
        drive(self.stack, decision, now_ms=BASE_TS + 5_001)
        follow_up = self._decide(now_ms=BASE_TS + 5_002, prediction=stale)
        drive(self.stack, follow_up, now_ms=BASE_TS + 5_003)

        active = self.stack.tracker.active()
        self.assertEqual([order.side for order in active], [Side.SELL])
        self.assertTrue(active[0].reduce_only)
        self.assertGreaterEqual(active[0].price, self.state.price.best_ask)
        self.assertEqual(self.stack.order(bid_order.client_order_id).status.value, "CANCELED")

    def test_fill_probability_does_not_change_the_quoted_size(self) -> None:
        baseline = self._decide(prediction=make_record(state=self.state, prediction=make_prediction(buy_fill_probability=0.01)))
        risky = self._decide(prediction=make_record(state=self.state, prediction=make_prediction(buy_fill_probability=0.99)))

        self.assertEqual(baseline.quotes(), risky.quotes())


if __name__ == "__main__":
    unittest.main()
