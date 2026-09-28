"""P0001.6.1：不确定订单暴露必须持续占用风险额度（SC-1 – SC-5）。

原则：**不知道订单是否存在时，Risk 必须假设它仍可能存在。**
"""

from __future__ import annotations

import unittest

from execution.types import ExternalOrder, OrderStatus
from portfolio.types import Side
from risk.gate import RiskGate
from risk.limits import RiskLimits
from risk.snapshot import build_risk_snapshot
from risk.types import OrderProposal, RiskDecisionType
from tests.execution_support import ExecutionStack
from tests.support import BASE_TS

LIMITS = RiskLimits(max_position_qty=10.0, max_open_order_exposure=120.0)


class UncertainExposureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExecutionStack.build(limits=LIMITS)
        self.order = self.stack.submit_order(self.stack.proposal(quantity=1.0, price=100.0), now_ms=BASE_TS)

    # ------------------------------------------------------------------ SC-1

    def test_sc1_total_exposure_does_not_drop_when_order_becomes_lost(self) -> None:
        before = self.stack.manager.open_order_exposure()
        self.stack.tracker.mark_lost(self.order.client_order_id, timestamp=BASE_TS + 1, reason="manual")

        after = self.stack.manager.open_order_exposure()

        self.assertAlmostEqual(before, 100.0)
        self.assertAlmostEqual(after, before, places=9)
        self.assertEqual(self.stack.manager.confirmed_open_exposure, 0.0)
        self.assertEqual(self.stack.manager.uncertain_exposure, 100.0)

    def test_snapshot_reports_the_split(self) -> None:
        self.stack.tracker.mark_lost(self.order.client_order_id, timestamp=BASE_TS + 1, reason="manual")

        snapshot = self.stack.engine.snapshot(self.stack.symbol, now_ms=BASE_TS + 2)

        self.assertAlmostEqual(snapshot.open_order_exposure, 100.0)
        self.assertAlmostEqual(snapshot.confirmed_open_exposure, 0.0)
        self.assertAlmostEqual(snapshot.uncertain_exposure, 100.0)
        self.assertEqual(snapshot.unresolved_order_count, 0)
        self.assertAlmostEqual(snapshot.available_balance, self.stack.accounting.balance - 100.0)

    # ------------------------------------------------------------------ SC-2

    def test_sc2_new_order_is_still_blocked_while_lost(self) -> None:
        self.stack.tracker.mark_lost(self.order.client_order_id, timestamp=BASE_TS + 1, reason="manual")

        result = self.stack.submit(self.stack.proposal(quantity=0.5, price=100.0), now_ms=BASE_TS + 2)

        self.assertTrue(result.rejected)
        self.assertIs(result.rejection.reason_code.value, "OPEN_ORDER_EXPOSURE_LIMIT")  # type: ignore[union-attr]
        self.assertEqual(len(self.stack.broker.open_orders()), 1)  # 新单从未发出

    def test_reduce_only_is_still_allowed_while_lost(self) -> None:
        from tests.support import make_fill

        self.stack.accounting.record_fill(make_fill("seed", Side.BUY, 100.0, 1.0, exchange_ts=BASE_TS))
        self.stack.tracker.mark_lost(self.order.client_order_id, timestamp=BASE_TS + 1, reason="manual")

        result = self.stack.submit(
            self.stack.proposal(side=Side.SELL, quantity=0.5, price=100.0, reduce_only=True), now_ms=BASE_TS + 2
        )

        self.assertTrue(result.submitted)

    # ------------------------------------------------------------------ SC-3

    def test_sc3_exposure_is_released_only_after_reconciliation_confirms_cancel(self) -> None:
        self.stack.tracker.mark_lost(self.order.client_order_id, timestamp=BASE_TS + 1, reason="ack timeout")
        self.assertAlmostEqual(self.stack.manager.open_order_exposure(), 100.0)

        # 外部仍然列出该订单（OPEN）→ 恢复为 active，暴露不释放
        restored = self.stack.reconcile(timestamp=BASE_TS + 2)
        self.assertIn("RESTORED", [kind.name for kind in restored.kinds()])
        self.assertIs(self.stack.order(self.order.client_order_id).status, OrderStatus.OPEN)
        self.assertAlmostEqual(self.stack.manager.open_order_exposure(), 100.0)

        # 外部确认已撤销 → 释放（只有明确确认才释放）
        from execution.events import OrderStatusUpdate

        self.stack.tracker.on_event(
            OrderStatusUpdate(self.order.client_order_id, OrderStatus.CANCELED, BASE_TS + 3, detail="confirmed")
        )

        self.assertEqual(self.stack.manager.open_order_exposure(), 0.0)
        self.assertEqual(self.stack.manager.uncertain_exposure, 0.0)

    def test_merely_disappearing_from_exchange_does_not_release_exposure(self) -> None:
        self.stack.tracker.mark_lost(self.order.client_order_id, timestamp=BASE_TS + 1, reason="ack timeout")
        self.stack.broker.drop_from_external(self.order.client_order_id)

        report = self.stack.reconcile(timestamp=BASE_TS + 2)

        self.assertEqual(report.corrective_actions, ())  # LOST 已是事实，无可纠正项
        self.assertAlmostEqual(self.stack.manager.open_order_exposure(), 100.0)  # 仍然占用额度

    # ------------------------------------------------------------------ SC-4

    def test_sc4_partial_fill_recomputes_remaining_exposure(self) -> None:
        self.stack.tracker.mark_lost(self.order.client_order_id, timestamp=BASE_TS + 1, reason="ack timeout")
        stalled = self.stack.order(self.order.client_order_id)

        # 外部报告部分成交 → reconciliation 校正数量并恢复为 active
        report = self.stack.reconcile(timestamp=BASE_TS + 2)

        self.assertIn("RESTORED", [kind.name for kind in report.kinds()])
        self.assertAlmostEqual(self.stack.manager.open_order_exposure(), 100.0)

        # 用外部成交补齐 0.4 → 剩余 0.6 × 100
        self.stack.broker.fill(stalled.client_order_id, quantity=0.4, price=100.0, timestamp=BASE_TS + 3)
        filled = self.stack.poll(now_ms=BASE_TS + 3)

        self.assertEqual([update.outcome.value for update in filled.updates], ["fill_applied"])
        self.assertAlmostEqual(self.stack.manager.confirmed_open_exposure, 60.0)
        self.assertAlmostEqual(self.stack.manager.uncertain_exposure, 0.0)
        self.assertAlmostEqual(self.stack.manager.open_order_exposure(), 60.0)

    def test_partial_fill_while_lost_is_applied_and_reduces_uncertain(self) -> None:
        self.stack.tracker.mark_lost(self.order.client_order_id, timestamp=BASE_TS + 1, reason="ack timeout")
        self.stack.broker.fill(self.order.client_order_id, quantity=0.3, price=100.0, timestamp=BASE_TS + 2)

        result = self.stack.poll(now_ms=BASE_TS + 2)

        self.assertEqual([update.outcome.value for update in result.updates], ["fill_applied"])
        order = self.stack.order(self.order.client_order_id)
        self.assertIs(order.status, OrderStatus.PARTIALLY_FILLED)  # LOST → active
        self.assertAlmostEqual(self.stack.manager.uncertain_exposure, 0.0)
        self.assertAlmostEqual(self.stack.manager.confirmed_open_exposure, 70.0)
        self.assertAlmostEqual(self.stack.accounting.position(self.stack.symbol).qty, 0.3)

    # ------------------------------------------------------------------ SC-5

    def test_sc5_insufficient_order_data_fails_closed(self) -> None:
        incomplete = ExternalOrder(
            client_order_id="probex-s1-000999",
            exchange_order_id=None,
            symbol=self.stack.symbol,
            status=OrderStatus.OPEN,
            filled_quantity=0.0,
            # 缺 side / quantity / price → 无法量化
        )
        tracker = self.stack.tracker

        from execution.reconciliation import reconcile

        report = reconcile(tracker, external_open_orders=(incomplete,), timestamp=BASE_TS + 10)

        self.assertIn("ADOPT_REJECTED", [kind.name for kind in report.kinds()])
        # 关键：不能「按 0 处理」，而要显式记录为 unresolved
        self.assertEqual([item.client_order_id for item in tracker.unresolved_orders()], ["probex-s1-000999"])
        self.assertTrue(tracker.has_unknown_exposure)

        snapshot = self.stack.engine.snapshot(self.stack.symbol, now_ms=BASE_TS + 11)
        self.assertEqual(snapshot.unresolved_order_count, 1)

        decision = RiskGate(LIMITS).evaluate(
            OrderProposal(symbol=self.stack.symbol, side=Side.BUY, quantity=0.1, price=100.0), snapshot
        )
        self.assertIs(decision.decision, RiskDecisionType.REJECT)
        self.assertIs(decision.reason_code.value, "UNCERTAIN_EXPOSURE_UNKNOWN")  # type: ignore[union-attr]

    def test_unresolved_exposure_does_not_block_reduce_only(self) -> None:
        from tests.support import make_fill

        self.stack.accounting.record_fill(make_fill("seed", Side.BUY, 100.0, 1.0, exchange_ts=BASE_TS))
        self.stack.tracker.note_unresolved_order("probex-s1-000999", reason="adopt failed")
        snapshot = self.stack.engine.snapshot(self.stack.symbol, now_ms=BASE_TS + 1)

        decision = RiskGate(LIMITS).evaluate(
            OrderProposal(
                symbol=self.stack.symbol, side=Side.SELL, quantity=0.5, price=100.0, reduce_only=True
            ),
            snapshot,
        )

        self.assertIs(decision.decision, RiskDecisionType.ALLOW)

    def test_resolved_order_clears_the_fail_closed_marker(self) -> None:
        self.stack.tracker.note_unresolved_order("probex-s1-000999", reason="adopt failed")
        self.assertTrue(self.stack.tracker.has_unknown_exposure)

        cleared = self.stack.tracker.clear_unresolved_order("probex-s1-000999")

        self.assertTrue(cleared)
        self.assertFalse(self.stack.tracker.has_unknown_exposure)
        self.assertFalse(self.stack.tracker.clear_unresolved_order("probex-s1-000999"))

    def test_successful_adoption_clears_the_marker(self) -> None:
        from execution.reconciliation import reconcile

        tracker = self.stack.tracker
        tracker.note_unresolved_order("probex-s1-000888", reason="adopt failed")
        complete = ExternalOrder(
            client_order_id="probex-s1-000888",
            exchange_order_id="paper-000888",
            symbol=self.stack.symbol,
            status=OrderStatus.OPEN,
            filled_quantity=0.0,
            side=Side.BUY,
            quantity=1.0,
            price=100.0,
        )

        report = reconcile(tracker, external_open_orders=(complete,), timestamp=BASE_TS + 10)

        self.assertIn("ADOPTED", [kind.name for kind in report.kinds()])
        self.assertFalse(tracker.has_unknown_exposure)
        self.assertAlmostEqual(tracker.confirmed_open_exposure(), 100.0)


class SnapshotConsistencyTest(unittest.TestCase):
    def test_inconsistent_exposure_inputs_are_rejected(self) -> None:
        from portfolio.accounting import AccountingCore

        accounting = AccountingCore(initial_balance=1_000.0)
        with self.assertRaises(ValueError):
            build_risk_snapshot(
                accounting,
                symbol="BTCUSDT",
                now_ms=BASE_TS,
                open_order_exposure=100.0,
                confirmed_open_exposure=80.0,
                uncertain_exposure=30.0,
            )

    def test_components_can_be_derived_from_total(self) -> None:
        from portfolio.accounting import AccountingCore

        accounting = AccountingCore(initial_balance=1_000.0)
        snapshot = build_risk_snapshot(
            accounting, symbol="BTCUSDT", now_ms=BASE_TS, open_order_exposure=100.0, uncertain_exposure=40.0
        )

        self.assertAlmostEqual(snapshot.open_order_exposure, 100.0)
        self.assertAlmostEqual(snapshot.confirmed_open_exposure, 60.0)
        self.assertAlmostEqual(snapshot.uncertain_exposure, 40.0)

    def test_invalid_component_values_rejected(self) -> None:
        from portfolio.accounting import AccountingCore

        accounting = AccountingCore(initial_balance=1_000.0)
        with self.assertRaises(ValueError):
            build_risk_snapshot(
                accounting, symbol="BTCUSDT", now_ms=BASE_TS, open_order_exposure=0.0, uncertain_exposure=-1.0
            )
        with self.assertRaises(ValueError):
            build_risk_snapshot(
                accounting, symbol="BTCUSDT", now_ms=BASE_TS, unresolved_order_count=-1
            )
        with self.assertRaises(ValueError):
            build_risk_snapshot(
                accounting, symbol="BTCUSDT", now_ms=BASE_TS, unresolved_order_count=True  # type: ignore[arg-type]
            )


if __name__ == "__main__":
    unittest.main()
