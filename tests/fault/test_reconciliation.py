"""Fault：重启 / reconciliation 收敛（SC-15）。"""

from __future__ import annotations

import unittest

from execution import OrderTracker
from execution.reconciliation import ReconciliationActionKind, reconcile
from execution.types import ExternalOrder, Order, OrderStatus
from market.events.types import Venue
from portfolio.accounting import AccountingCore
from portfolio.types import Side
from risk.limits import RiskLimits
from tests.execution_support import ExecutionStack
from tests.support import BASE_TS


def _local_snapshot(client_order_id: str, **overrides: object) -> Order:
    values: dict[str, object] = {
        "client_order_id": client_order_id,
        "venue": Venue.BINANCE,
        "symbol": "BTCUSDT",
        "side": Side.BUY,
        "price": 100.0,
        "quantity": 1.0,
        "status": OrderStatus.OPEN,
        "created_at": BASE_TS,
        "updated_at": BASE_TS,
    }
    values.update(overrides)
    return Order(**values)  # type: ignore[arg-type]


class RestartReconciliationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExecutionStack.build(limits=RiskLimits(max_position_qty=10.0))
        self.order = self.stack.submit_order(self.stack.proposal(quantity=1.0), now_ms=BASE_TS)

    def test_sc15_restart_converges_with_broker_facts(self) -> None:
        self.stack.fill(self.order, quantity=0.4, fee=0.04, timestamp=BASE_TS + 1)

        # 「重启」：新的 tracker 只有陈旧快照（OPEN，filled 0.0），accounting 空
        restarted = OrderTracker(session_id="s1")
        restarted.register(_local_snapshot(self.order.client_order_id))
        restarted_accounting = AccountingCore(initial_balance=10_000.0)

        report = reconcile(
            restarted,
            external_open_orders=self.stack.broker.open_orders(),
            external_recent_fills=self.stack.broker.recent_fills(),
            timestamp=BASE_TS + 10,
        )
        for fill in report.canonical_fills:
            restarted_accounting.record_fill(fill)

        order = restarted.require_order(self.order.client_order_id)
        self.assertIs(order.status, OrderStatus.PARTIALLY_FILLED)
        self.assertAlmostEqual(order.filled_quantity, 0.4)
        self.assertAlmostEqual(restarted_accounting.position("BTCUSDT").qty, 0.4)

        # 再跑一次 → 收敛
        second = reconcile(
            restarted,
            external_open_orders=self.stack.broker.open_orders(),
            external_recent_fills=self.stack.broker.recent_fills(),
            timestamp=BASE_TS + 20,
        )
        self.assertTrue(second.converged)
        self.assertEqual(second.corrective_actions, ())

    def test_terminal_order_after_restart_is_reconciled(self) -> None:
        self.stack.cancel(self.order, now_ms=BASE_TS + 1)

        restarted = OrderTracker(session_id="s1")
        restarted.register(_local_snapshot(self.order.client_order_id))  # 陈旧：仍以为 OPEN

        report = reconcile(
            restarted,
            external_open_orders=self.stack.broker.open_orders(),
            external_recent_fills=self.stack.broker.recent_fills(),
            timestamp=BASE_TS + 10,
        )

        self.assertIn(ReconciliationActionKind.MARKED_LOST, report.kinds())
        # 本地以为 OPEN、外部已撤销 → 先 LOST 再让 operator 决策（本阶段不自动改终态）
        self.assertTrue(restarted.require_order(self.order.client_order_id).is_lost)

    def test_unknown_external_order_is_adopted(self) -> None:
        external = self.stack.broker.open_orders()[0]
        unknown = ExternalOrder(
            client_order_id="probex-s1-000777",
            exchange_order_id="paper-000777",
            symbol="BTCUSDT",
            status=OrderStatus.OPEN,
            filled_quantity=0.1,
            avg_fill_price=100.0,
            side=Side.BUY,
            quantity=1.0,
            price=100.0,
        )
        tracker = OrderTracker(session_id="s1")

        report = reconcile(tracker, external_open_orders=(unknown,), timestamp=BASE_TS + 10)

        self.assertIn(ReconciliationActionKind.ADOPTED, report.kinds())
        adopted = tracker.require_order("probex-s1-000777")
        self.assertIs(adopted.status, OrderStatus.OPEN)
        self.assertEqual(adopted.exchange_order_id, "paper-000777")
        self.assertAlmostEqual(adopted.filled_quantity, 0.1)
        self.assertIsNotNone(external.client_order_id)

    def test_adoption_fails_closed_without_quantity_or_price(self) -> None:
        tracker = OrderTracker(session_id="s1")
        incomplete = ExternalOrder(
            client_order_id="probex-s1-000778",
            exchange_order_id="paper-000778",
            symbol="BTCUSDT",
            status=OrderStatus.OPEN,
            filled_quantity=0.0,
        )

        report = reconcile(tracker, external_open_orders=(incomplete,), timestamp=BASE_TS + 10)

        self.assertIn(ReconciliationActionKind.ADOPT_REJECTED, report.kinds())
        self.assertFalse(report.converged)
        self.assertIsNone(tracker.order("probex-s1-000778"))

    def test_quantity_correction_when_fill_events_are_missing(self) -> None:
        tracker = OrderTracker(session_id="s1")
        tracker.register(_local_snapshot(self.order.client_order_id))
        ahead = ExternalOrder(
            client_order_id=self.order.client_order_id,
            exchange_order_id=self.order.exchange_order_id,
            symbol="BTCUSDT",
            status=OrderStatus.PARTIALLY_FILLED,
            filled_quantity=0.5,
            avg_fill_price=100.0,
            side=Side.BUY,
            quantity=1.0,
            price=100.0,
        )

        report = reconcile(tracker, external_open_orders=(ahead,), timestamp=BASE_TS + 10)

        self.assertIn(ReconciliationActionKind.QUANTITY_CORRECTED, report.kinds())
        corrected = tracker.require_order(self.order.client_order_id)
        self.assertAlmostEqual(corrected.filled_quantity, 0.5)
        self.assertIs(corrected.status, OrderStatus.PARTIALLY_FILLED)

    def test_status_correction_for_stale_local_status(self) -> None:
        tracker = OrderTracker(session_id="s1")
        tracker.register(_local_snapshot(self.order.client_order_id))
        canceled_externally = ExternalOrder(
            client_order_id=self.order.client_order_id,
            exchange_order_id=self.order.exchange_order_id,
            symbol="BTCUSDT",
            status=OrderStatus.CANCELED,
            filled_quantity=0.0,
            side=Side.BUY,
            quantity=1.0,
            price=100.0,
        )

        report = reconcile(tracker, external_open_orders=(canceled_externally,), timestamp=BASE_TS + 10)

        self.assertIn(ReconciliationActionKind.STATUS_CORRECTED, report.kinds())
        self.assertIs(tracker.require_order(self.order.client_order_id).status, OrderStatus.CANCELED)

    def test_reconcile_of_unknown_fill_is_reported_not_accounted(self) -> None:
        tracker = OrderTracker(session_id="s1")
        report = reconcile(
            tracker,
            external_recent_fills=self.stack.broker.recent_fills(),
            timestamp=BASE_TS + 10,
        )

        self.assertEqual(report.canonical_fills, ())


if __name__ == "__main__":
    unittest.main()
