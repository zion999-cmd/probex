"""Integration：执行层与 Risk 的连接点（SC-10 – SC-13）。"""

from __future__ import annotations

import unittest

from execution import ExecutionEngine, OrderManager, OrderTracker, PaperBroker
from portfolio.accounting import AccountingCore
from portfolio.types import Side
from risk.gate import RiskGate
from risk.limits import KillSwitchMode, RiskLimits
from risk.types import OrderProposal
from tests.execution_support import ExecutionStack
from tests.support import BASE_TS


class PendingExposureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExecutionStack.build(limits=RiskLimits(max_position_qty=10.0, max_open_order_exposure=120.0))

    def test_sc10_active_unfilled_quantity_enters_open_order_exposure(self) -> None:
        order = self.stack.submit_order(self.stack.proposal(quantity=1.0, price=100.0), now_ms=BASE_TS)

        snapshot = self.stack.engine.snapshot(self.stack.symbol, now_ms=BASE_TS)

        self.assertAlmostEqual(self.stack.manager.open_order_exposure(), 100.0)
        self.assertAlmostEqual(snapshot.open_order_exposure, 100.0)
        self.assertAlmostEqual(snapshot.available_balance, self.stack.accounting.balance - 100.0)
        self.assertEqual(snapshot.position_qty, 0.0)
        self.assertIs(self.stack.order(order.client_order_id).status.value, "OPEN")

    def test_partial_fill_reduces_pending_exposure(self) -> None:
        order = self.stack.submit_order(self.stack.proposal(quantity=1.0, price=100.0), now_ms=BASE_TS)
        self.stack.fill(order, quantity=0.6, timestamp=BASE_TS + 1)

        self.assertAlmostEqual(self.stack.manager.open_order_exposure(), 40.0)
        snapshot = self.stack.engine.snapshot(self.stack.symbol, now_ms=BASE_TS + 2)
        self.assertAlmostEqual(snapshot.open_order_exposure, 40.0)
        self.assertAlmostEqual(snapshot.position_qty, 0.6)

    def test_sc11_second_order_cannot_hide_behind_unfilled_position(self) -> None:
        self.stack.submit_order(self.stack.proposal(quantity=1.0, price=100.0), now_ms=BASE_TS)
        self.assertEqual(self.stack.accounting.position(self.stack.symbol).qty, 0.0)

        second = self.stack.submit(self.stack.proposal(quantity=0.5, price=100.0), now_ms=BASE_TS + 1)

        self.assertTrue(second.rejected)
        self.assertIs(second.rejection.reason_code.value, "OPEN_ORDER_EXPOSURE_LIMIT")  # type: ignore[union-attr]
        self.assertEqual(len(self.stack.broker.open_orders()), 1)  # 第二单从未发给 broker

    def test_canceled_order_releases_pending_exposure(self) -> None:
        order = self.stack.submit_order(self.stack.proposal(quantity=1.0, price=100.0), now_ms=BASE_TS)
        self.stack.cancel(order, now_ms=BASE_TS + 1)

        self.assertEqual(self.stack.manager.open_order_exposure(), 0.0)
        retry = self.stack.submit(self.stack.proposal(quantity=1.0, price=100.0), now_ms=BASE_TS + 2)
        self.assertTrue(retry.submitted)


class KillSwitchExecutionTest(unittest.TestCase):
    def _engine(self, mode: KillSwitchMode, stack: ExecutionStack) -> ExecutionEngine:
        return ExecutionEngine(
            accounting=stack.accounting,
            gate=RiskGate(RiskLimits(kill_switch_mode=mode, max_position_qty=10.0)),
            manager=stack.manager,
        )

    def test_sc12_reduce_only_mode_allows_only_exposure_reduction(self) -> None:
        stack = ExecutionStack.build(limits=RiskLimits(max_position_qty=10.0))
        engine = self._engine(KillSwitchMode.REDUCE_ONLY, stack)
        stack.accounting.record_fill(
            __import__("tests.support", fromlist=["make_fill"]).make_fill(
                "seed", Side.BUY, 100.0, 1.0, exchange_ts=BASE_TS
            )
        )

        increasing = engine.submit(
            OrderProposal(symbol=stack.symbol, side=Side.BUY, quantity=0.5, price=100.0), now_ms=BASE_TS + 1
        )
        reducing = engine.submit(
            OrderProposal(symbol=stack.symbol, side=Side.SELL, quantity=0.5, price=100.0, reduce_only=True),
            now_ms=BASE_TS + 2,
        )

        self.assertTrue(increasing.rejected)
        self.assertIs(increasing.rejection.reason_code.value, "KILL_SWITCH")  # type: ignore[union-attr]
        self.assertTrue(reducing.submitted)
        self.assertEqual(len(stack.broker.open_orders()), 1)

    def test_sc13_halt_all_blocks_submit_but_allows_cancel(self) -> None:
        stack = ExecutionStack.build(limits=RiskLimits(max_position_qty=10.0))
        existing = stack.submit_order(stack.proposal(quantity=1.0), now_ms=BASE_TS)
        halted = self._engine(KillSwitchMode.HALT_ALL, stack)

        blocked = halted.submit(stack.proposal(quantity=0.5), now_ms=BASE_TS + 1)
        self.assertTrue(blocked.rejected)
        self.assertIs(blocked.rejection.reason_code.value, "KILL_SWITCH")  # type: ignore[union-attr]
        self.assertEqual(len(stack.broker.open_orders()), 1)

        canceled = halted.cancel(existing.client_order_id, now_ms=BASE_TS + 2)
        self.assertEqual([update.outcome.value for update in canceled.updates], ["order_canceled"])
        self.assertIs(stack.order(existing.client_order_id).status.value, "CANCELED")

    def test_p00015_bool_kill_switch_keeps_halt_all_semantics(self) -> None:
        limits = RiskLimits(kill_switch=True)
        self.assertIs(limits.effective_kill_switch_mode, KillSwitchMode.HALT_ALL)
        self.assertIn("kill_switch", limits.enabled_checks())

    def test_normal_mode_allows_increasing_orders(self) -> None:
        stack = ExecutionStack.build(limits=RiskLimits(max_position_qty=10.0))
        engine = self._engine(KillSwitchMode.NORMAL, stack)

        result = engine.submit(stack.proposal(quantity=1.0), now_ms=BASE_TS)

        self.assertTrue(result.submitted)


class RiskBeforeSubmitTest(unittest.TestCase):
    def test_fresh_snapshot_is_taken_at_submit_time(self) -> None:
        stack = ExecutionStack.build(limits=RiskLimits(max_position_qty=2.0))
        engine = stack.engine

        first = engine.submit(stack.proposal(quantity=1.0), now_ms=BASE_TS)
        self.assertTrue(first.submitted)
        assert first.snapshot is not None
        self.assertAlmostEqual(first.snapshot.open_order_exposure, 0.0)

        second = engine.submit(stack.proposal(quantity=1.5), now_ms=BASE_TS + 1)
        assert second.snapshot is not None
        # 第二次快照必须看到第一次的 pending exposure（不是过期快照）
        self.assertAlmostEqual(second.snapshot.open_order_exposure, first.order.notional)  # type: ignore[union-attr]
        self.assertTrue(second.submitted)

    def test_rejections_are_logged_for_telemetry(self) -> None:
        stack = ExecutionStack.build(limits=RiskLimits(max_position_qty=0.1))
        engine = stack.engine

        engine.submit(stack.proposal(quantity=1.0), now_ms=BASE_TS)

        self.assertEqual(len(engine.rejections), 1)
        self.assertEqual(engine.rejections[0].reason_code.value, "POSITION_LIMIT")  # type: ignore[union-attr]

    def test_book_health_gate_is_honoured(self) -> None:
        stack = ExecutionStack.build(limits=RiskLimits(max_position_qty=10.0), book_healthy=False)

        result = stack.submit(stack.proposal(quantity=0.1), now_ms=BASE_TS)

        self.assertTrue(result.rejected)
        self.assertIs(result.rejection.reason_code.value, "BOOK_UNHEALTHY")  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()
