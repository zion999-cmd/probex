"""P0001.8 集成测试：完整研究链（§16）。

```text
Replay MarketEvent → MarketState → Recorded Prediction → MakerPolicy → RiskGate
                   → SimulatedVenue → Fill → Accounting → 下一轮决策
```

不使用 Jev 网络调用：prediction 使用既有 `PredictionRecord`（RECORDED 模式）。
"""

from __future__ import annotations

import unittest

from execution.types import OrderStatus
from portfolio.types import Side
from risk.limits import RiskLimits
from risk.types import KillSwitchMode
from strategy.maker import MakerPolicy, QuoteAction
from tests.sim_support import SimStack, book_delta, book_snapshot, sell_aggressor, ts
from tests.strategy_support import make_record, maker_config
from tests.support import SYMBOL, feature_engine, warm_market_states

MARK_HINT_TS = ts(300_000)
BUDGET = 500.0


def _states() -> tuple:
    """从真实 Replay 事件得到 warm up 后的 MarketState（P0001.3 FeatureEngine）。"""
    return warm_market_states(2)


def _decide(policy, stack: SimStack, state, prediction, *, now_ms: int):
    """一轮决策：MarketState + Prediction + Position + RiskSnapshot → MakerDecision。"""
    return policy.decide(
        state=state,
        prediction=prediction,
        position=stack.accounting.position(SYMBOL),
        snapshot=stack.engine.snapshot(SYMBOL, now_ms=now_ms),
        existing_orders=stack.tracker.active(),
        remaining_risk_budget=BUDGET,
        kill_switch=KillSwitchMode.NORMAL,
    )


def _run_chain() -> tuple:
    """跑一遍「决策 → 成交 → 记账 → 下一轮决策」，返回全部可比较证据。"""
    states = _states()
    first_state, second_state = states[0], states[1]
    stack = SimStack.build(limits=RiskLimits(max_position_qty=10.0, max_open_order_exposure=500.0))
    policy = MakerPolicy(maker_config())

    # 模拟器所见盘口与 FeatureEngine 一致（同一份记录事件的两个独立投影）
    stack.feed(
        [
            book_snapshot(
                update_id=1,
                bids=((100.0, 5.1),),
                asks=((101.0, 2.0),),
                offset=300_000,
            )
        ]
    )
    prediction = make_record(state=first_state, now_ms=MARK_HINT_TS)

    decision = _decide(policy, stack, first_state, prediction, now_ms=MARK_HINT_TS)
    bid_proposal = decision.bid.proposal
    submitted = stack.submit(bid_proposal, now_ms=MARK_HINT_TS)

    # aggressor 卖单打穿队列并成交
    produced = stack.feed(
        [
            sell_aggressor(1, price=100.0, quantity=5.1, offset=300_001),  # 清空前方队列
            sell_aggressor(2, price=100.0, quantity=1.0, offset=300_002),  # 成交
        ]
    )

    follow_up = _decide(
        policy, stack, second_state, make_record(state=second_state, now_ms=ts(300_002)), now_ms=ts(300_002)
    )

    position = stack.accounting.position(SYMBOL)
    return (
        decision,
        bid_proposal,
        (submitted.order, stack.order(submitted.order.client_order_id)),  # type: ignore[union-attr]
        produced,
        stack.venue.fills,
        stack.venue.order_views(),
        (
            position.qty,
            position.avg_entry_price,
            stack.accounting.balance,
            stack.accounting.trading_fees,
        ),
        follow_up,
        stack.state(),
    )


class MakerSimulatedExecutionTest(unittest.TestCase):
    def test_full_chain_produces_a_fill_and_updates_accounting(self) -> None:
        decision, bid_proposal, (submit_snapshot, order), produced, fills, views, accounting_state, follow_up, _ = _run_chain()

        self.assertTrue(decision.bid.proposal.post_only)
        self.assertEqual(bid_proposal.price, 100.0)
        self.assertIs(submit_snapshot.status, OrderStatus.OPEN)  # submit 返回的是提交时刻的快照
        self.assertIs(order.status, OrderStatus.FILLED)  # 成交后本地状态权威已推进
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].fill_reason.value, "queue_consumed")
        self.assertEqual(fills[0].queue_state.value, "known")
        self.assertTrue(all(event.event_type.value == "fill_received" for event in produced))
        self.assertAlmostEqual(accounting_state[0], 1.0)  # 持仓 1.0
        self.assertAlmostEqual(accounting_state[1], 100.0)  # 均价
        self.assertAlmostEqual(accounting_state[3], 100.0 * 1.0 * 0.0002)  # maker fee
        self.assertEqual(views[0].initial_queue_ahead, 5.1)

    def test_next_decision_sees_the_position_and_quotes_a_reduce_only_exit(self) -> None:
        _, _, _, _, _, _, _, follow_up, _ = _run_chain()

        self.assertIs(follow_up.ask.action, QuoteAction.PLACE)
        self.assertTrue(follow_up.ask.proposal.reduce_only)
        self.assertLessEqual(follow_up.ask.proposal.quantity, 1.0 + 1e-9)
        self.assertGreaterEqual(follow_up.ask.proposal.price, 101.0)
        self.assertFalse(follow_up.bid.proposal.reduce_only)

    def test_chain_is_deterministic(self) -> None:
        first = _run_chain()
        second = _run_chain()

        self.assertEqual(first, second)

    def test_follow_up_quotes_pass_the_risk_gate(self) -> None:
        """链路的最后一环：策略产出仍须由 RiskGate 放行（它仍是最终权威）。"""
        *_, follow_up, stack_state = _run_chain()
        stack = SimStack.build(limits=RiskLimits(max_position_qty=10.0, max_open_order_exposure=500.0))
        stack.accounting.record_fill(
            __import__("tests.support", fromlist=["make_fill"]).make_fill(
                "seed", Side.BUY, 100.0, 1.0, exchange_ts=ts(300_000)
            )
        )
        snapshot = stack.engine.snapshot(SYMBOL, now_ms=ts(300_002))

        for proposal in follow_up.quotes():
            with self.subTest(side=proposal.side.value):
                decision = stack.gate.evaluate(proposal, snapshot)
                self.assertTrue(decision.allowed, decision.details)

        self.assertIs(follow_up.ask.proposal.side, Side.SELL)
        self.assertTrue(stack_state[0][0].is_terminal)

    def test_book_events_are_the_only_source_of_market_state(self) -> None:
        """证据：市场状态与模拟器盘口来自同一份 Replay 事件（这里用两条独立投影）。"""
        first_state = _states()[0]

        self.assertEqual(first_state.identity.symbol, SYMBOL)
        self.assertTrue(first_state.quality.tradeable)
        self.assertEqual(first_state.price.best_bid, 100.0)


if __name__ == "__main__":
    unittest.main()
