"""P0001.9.7 集成：多轮 live loop 的稳定性与不变量（SC-27 – SC-31、SC-33）。

用可控的 market/prediction 序列驱动若干轮，检查：

- 无重复 submit（同一 quote 不会被反复重下）；
- 无 orphan / 无同侧多 active order；
- telemetry 累计计数与账户状态自洽；
- reconciliation 只在触发条件下运行。
"""

from __future__ import annotations

import unittest

from execution.types import OrderStatus
from live import OrchestratorState
from portfolio.position import Position
from strategy.maker.types import QuoteAction
from tests.orchestration_support import NOW, OrchestrationStack
from tests.strategy_support import make_record, market_state
from tests.support import SYMBOL


def record(*, ttl_ms: int = 600_000):
    return make_record(now_ms=NOW, ttl_ms=ttl_ms)


def healthy(bid: float = 60_000.0, ask: float = 60_010.0):
    return market_state(best_bid=bid, best_ask=ask)


class MultiRoundStabilityTest(unittest.TestCase):
    def test_repeated_identical_rounds_do_not_churn(self) -> None:
        """同价同预测重复 5 轮：只应有一次 PLACE（后续 KEEP），零撤单。"""
        stack = OrchestrationStack.build()
        rec = record()

        for _ in range(5):
            stack.orchestrator.run_loop(state=healthy(), prediction=rec)

        totals = stack.orchestrator.telemetry.totals
        self.assertEqual(totals["submit_count"], 2)  # 双边各一次
        self.assertEqual(totals["cancel_count"], 0)
        self.assertEqual(len(stack.engine.manager.active_orders), 2)
        self.assertEqual(stack.orchestrator.multiplicity_violations, 0)

    def test_price_move_triggers_replace_but_never_exceeds_two_active(self) -> None:
        stack = OrchestrationStack.build()
        rec = record()
        stack.orchestrator.run_loop(state=healthy(), prediction=rec)

        for _ in range(3):
            stack.orchestrator.run_loop(state=healthy(bid=60_500.0, ask=60_510.0), prediction=rec)

        totals = stack.orchestrator.telemetry.totals
        self.assertGreaterEqual(totals["replace_count"], 1)
        self.assertLessEqual(len(stack.engine.manager.active_orders), 2)
        self.assertEqual(stack.orchestrator.multiplicity_violations, 0)

    def test_market_unhealthy_cancels_increasing_risk_quotes(self) -> None:
        stack = OrchestrationStack.build()
        rec = record()
        stack.orchestrator.run_loop(state=healthy(), prediction=rec)

        stack.orchestrator.run_loop(state=market_state(tradeable=False), prediction=rec)

        self.assertEqual(stack.engine.manager.active_orders, ())
        self.assertEqual(stack.orchestrator.telemetry.totals["cancel_count"], 2)

    def test_no_orphan_orders_after_stop(self) -> None:
        stack = OrchestrationStack.build()
        rec = record()
        stack.orchestrator.run_loop(state=healthy(), prediction=rec)

        report = stack.orchestrator.stop(position_qty=0.0)

        self.assertEqual(report.remaining_active, ())
        self.assertEqual(report.cancelled, tuple(sorted(report.cancelled)))  # 有序、无重复
        self.assertEqual(len(set(report.cancelled)), len(report.cancelled))
        self.assertEqual(
            [order for order in stack.tracker.orders if order.status.is_active and order.status is not OrderStatus.LOST],
            [],
        )

    def test_telemetry_explains_each_round(self) -> None:
        stack = OrchestrationStack.build()
        rec = record()
        stack.orchestrator.run_loop(state=healthy(), prediction=rec)
        stack.orchestrator.run_loop(state=market_state(tradeable=False), prediction=rec)

        first, second = stack.orchestrator.telemetry.records
        self.assertEqual(first.bid_action, QuoteAction.PLACE.value)
        self.assertEqual(first.submit_count, 2)
        self.assertEqual(second.bid_action, QuoteAction.CANCEL.value)
        self.assertEqual(second.cancel_count, 2)
        self.assertEqual(first.authority_id, "auth-1")
        self.assertTrue(first.authority_valid)

    def test_reconciliation_only_on_trigger(self) -> None:
        stack = OrchestrationStack.build()
        rec = record()
        for _ in range(3):
            stack.orchestrator.run_loop(state=healthy(), prediction=rec)

        self.assertEqual(stack.orchestrator.telemetry.reconciliation_runs, 0)  # 正常轮次不触发

        outcome = stack.orchestrator.reconcile_now(reason="explicit")

        # 假 provider 报告"交易所有 0 挂单"，而本地有 2 笔 active ⇒ 真实分歧 ⇒ 标记 LOST 且**不收敛**
        self.assertFalse(outcome.converged)
        self.assertEqual(outcome.triggered_by, "explicit")
        self.assertIn("marked_lost", outcome.detail)
        self.assertEqual(stack.orchestrator.telemetry.reconciliation_runs, 0)  # telemetry 是**每轮**记录


class PositionConsistencyTest(unittest.TestCase):
    def test_position_is_derived_from_accounting(self) -> None:
        stack = OrchestrationStack.build()
        rec = record()
        stack.accounting.record_fill(
            __import__("tests.support", fromlist=["make_fill"]).make_fill(
                "f1", __import__("portfolio.types", fromlist=["Side"]).Side.BUY, 60_000.0, 0.001
            )
        )

        outcome = stack.orchestrator.run_loop(state=healthy(), prediction=rec)

        self.assertAlmostEqual(outcome.telemetry.position_qty, 0.001)

    def test_inconsistent_position_argument_is_refused(self) -> None:
        from live import OrchestratorError

        stack = OrchestrationStack.build()

        with self.assertRaises(OrchestratorError):
            stack.orchestrator.run_loop(
                state=healthy(), prediction=record(),
                position=Position(symbol=SYMBOL, qty=0.5, avg_entry_price=60_000.0, mark_price=60_000.0),
            )


if __name__ == "__main__":
    unittest.main()
