"""Replay：同一输入序列重放两次必须得到完全相同的 Maker 决策（SC-12）。"""

from __future__ import annotations

import unittest

from portfolio.types import Side
from risk.limits import RiskLimits
from risk.types import KillSwitchMode
from strategy.maker import MakerPolicy, QuoteAction
from tests.execution_support import ExecutionStack
from tests.strategy_support import make_prediction, make_record, maker_config, market_state
from tests.support import BASE_TS, SYMBOL


def _script() -> tuple:
    """一段包含报价、成交、库存变化、盘口移动、预测过期与熔断的脚本。"""
    stack = ExecutionStack.build(limits=RiskLimits(max_position_qty=10.0, max_open_order_exposure=500.0))
    policy = MakerPolicy(maker_config())
    decisions: list[tuple] = []

    def decide(*, state, prediction, now_ms: int, budget: float | None = 1_000.0, kill_switch=KillSwitchMode.NORMAL):
        decision = policy.decide(
            state=state,
            prediction=prediction,
            position=stack.accounting.position(SYMBOL),
            snapshot=stack.engine.snapshot(SYMBOL, now_ms=now_ms),
            existing_orders=stack.tracker.active(),
            remaining_risk_budget=budget,
            kill_switch=kill_switch,
        )
        decisions.append((decision, tuple(order.client_order_id for order in stack.tracker.active())))
        return decision

    state = market_state()
    prediction = make_record(state=state)
    first = decide(state=state, prediction=prediction, now_ms=BASE_TS)
    for side_decision in first.sides:
        stack.engine.submit(side_decision.proposal, now_ms=BASE_TS)

    bid_order = next(order for order in stack.tracker.active() if order.side is Side.BUY)
    stack.fill(bid_order, quantity=0.5, price=bid_order.price, timestamp=BASE_TS + 1)

    moved = market_state(best_bid=100.03, best_ask=101.03)
    second = decide(state=moved, prediction=make_record(state=moved, now_ms=BASE_TS + 1), now_ms=BASE_TS + 2)
    for side_decision in second.sides:
        if side_decision.action is QuoteAction.CANCEL:
            stack.engine.cancel(side_decision.client_order_id, now_ms=BASE_TS + 2)
        elif side_decision.action is QuoteAction.PLACE:
            stack.engine.submit(side_decision.proposal, now_ms=BASE_TS + 2)
        elif side_decision.action is QuoteAction.REPLACE:
            stack.manager.replace(side_decision.client_order_id, side_decision.proposal, timestamp=BASE_TS + 2)

    skewed = market_state(best_bid=100.03, best_ask=101.03, imbalance_5=-0.9, microprice=99.0)
    third = decide(
        state=skewed,
        prediction=make_record(
            state=skewed,
            now_ms=BASE_TS + 2,
            prediction=make_prediction(buy_adverse_selection=0.5, strong_down=0.3, down=0.3, flat=0.2, up=0.1, strong_up=0.1),
        ),
        now_ms=BASE_TS + 3,
    )
    decide(state=skewed, prediction=make_record(state=skewed, now_ms=BASE_TS, ttl_ms=1_000), now_ms=BASE_TS + 10_000)
    decide(state=skewed, prediction=None, now_ms=BASE_TS + 10_001)
    decide(state=skewed, prediction=make_record(state=skewed), now_ms=BASE_TS + 10_002, budget=None)
    decide(
        state=skewed,
        prediction=make_record(state=skewed),
        now_ms=BASE_TS + 10_003,
        kill_switch=KillSwitchMode.HALT_ALL,
    )
    decide(
        state=skewed,
        prediction=make_record(state=skewed),
        now_ms=BASE_TS + 10_004,
        kill_switch=KillSwitchMode.REDUCE_ONLY,
    )

    return (
        tuple(decisions),
        third,
        stack.state(),
        tuple(order.status.value for order in stack.tracker.orders),
    )


class MakerDeterminismTest(unittest.TestCase):
    def test_two_replays_are_field_identical(self) -> None:
        first = _script()
        second = _script()

        self.assertEqual(first, second)

    def test_decisions_are_value_objects(self) -> None:
        _, third, _, _ = _script()

        self.assertEqual(third, _script()[1])
        self.assertTrue(third.quotes())
        self.assertEqual(third.cancels(), ())


if __name__ == "__main__":
    unittest.main()
