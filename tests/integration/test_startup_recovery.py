"""P0001.9.3 集成测试：启动恢复编排（SC-1 / SC-2 / SC-3 / SC-6 / SC-9）。"""

from __future__ import annotations

import unittest

from execution.events import OrderAccepted, OrderCanceled, OrderStatusUpdate
from execution.reconciliation import ReconciliationActionKind
from execution.types import OrderStatus
from market.events.types import Venue
from portfolio.types import Side
from risk.limits import RiskLimits
from risk.types import OrderProposal, RiskDecisionType

from connectors.binance.private.recovery import RecoveryReason, RecoveryStatus
from tests.private_support import (
    SYMBOL,
    binance_order_payload,
    binance_trade_payload,
    build_recovery,
    position_risk_payload,
    recovery_responses,
    stream_state,
)
from tests.support import BASE_TS


def _flat_position() -> list[dict]:
    return position_risk_payload(position_amt="0", entry_price="0", mark_price="0")


def _long_position(qty: str = "0.001") -> list[dict]:
    return position_risk_payload(position_amt=qty, entry_price="60000", mark_price="60000")


def _open_local(tracker, *, quantity: float = 0.002, price: float = 60_000.0):
    """建立一笔合法的本地 OPEN 订单（create → accepted）。"""
    order = tracker.create(
        OrderProposal(symbol=SYMBOL, side=Side.BUY, quantity=quantity, price=price), timestamp=BASE_TS - 20
    )
    tracker.on_event(
        OrderAccepted(client_order_id=order.client_order_id, timestamp=BASE_TS - 19,
                      exchange_order_id="101")
    )
    return tracker.require_order(order.client_order_id)


def _cancel_local(tracker, client_order_id: str) -> None:
    tracker.on_event(OrderCanceled(client_order_id=client_order_id, timestamp=BASE_TS - 5))


class RecoveryBasicsTest(unittest.TestCase):
    def test_sc1_empty_account_with_no_orders_recovers(self) -> None:
        recovery, _, tracker, accounting = build_recovery(responses=recovery_responses(position=_flat_position()))

        result = recovery.run(stream_state=stream_state(), snapshot_provider=recovery.fetch_snapshot)

        self.assertIs(result.status, RecoveryStatus.RECOVERED)
        self.assertEqual(result.reasons, ())
        self.assertTrue(accounting.baseline_applied)
        self.assertFalse(accounting.historical_pnl_known)

    def test_snapshot_facts_are_normalized(self) -> None:
        recovery, _, _, _ = build_recovery()

        snapshot = recovery.fetch_snapshot()

        self.assertEqual(snapshot.symbol, SYMBOL)
        self.assertEqual(snapshot.foreign_open_orders, ())
        self.assertEqual(snapshot.fills, ())
        self.assertAlmostEqual(snapshot.account.total_wallet_balance, 1000.50)

    def test_sc6_non_flat_position_uses_baseline_without_synthetic_fill(self) -> None:
        recovery, _, _, accounting = build_recovery()

        result = recovery.run(stream_state=stream_state(), snapshot_provider=recovery.fetch_snapshot)

        self.assertIs(result.status, RecoveryStatus.RECOVERED)
        self.assertIsNotNone(result.baseline)
        self.assertEqual(len(accounting.fills.fills), 0)
        self.assertAlmostEqual(accounting.position(SYMBOL).qty, 0.5)
        self.assertAlmostEqual(accounting.balance, 1000.50)

    def test_baseline_makes_risk_gate_fail_closed_for_daily_loss(self) -> None:
        """SC-6 的延伸：baseline 之后今日 PnL / drawdown 未知 ⇒ RiskGate 必须 fail closed。"""
        from risk.gate import RiskGate

        recovery, _, _, accounting = build_recovery()
        recovery.run(stream_state=stream_state(), snapshot_provider=recovery.fetch_snapshot)
        accounting.update_mark_price(SYMBOL, 60_100.0, timestamp=BASE_TS)

        from risk.snapshot import build_risk_snapshot

        snapshot = build_risk_snapshot(accounting, symbol=SYMBOL, now_ms=BASE_TS, day_start_ts=BASE_TS - 1)
        proposal = OrderProposal(symbol=SYMBOL, side=Side.BUY, quantity=0.001, price=60_000.0)

        decision = RiskGate(RiskLimits(max_daily_loss=100.0, max_drawdown_pct=0.5)).evaluate(proposal, snapshot)

        self.assertIs(decision.decision, RiskDecisionType.REJECT)
        self.assertTrue(decision.reason_code.value.startswith("MISSING_"), decision.reason_code.value)


class OrderRecoveryTest(unittest.TestCase):
    def test_sc2_unknown_external_open_order_is_adopted(self) -> None:
        responses = recovery_responses(
            position=_flat_position(), open_orders=[binance_order_payload()]
        )
        recovery, _, tracker, accounting = build_recovery(responses=responses)

        result = recovery.run(stream_state=stream_state(), snapshot_provider=recovery.fetch_snapshot)

        self.assertIs(result.status, RecoveryStatus.RECOVERED)
        self.assertIn(ReconciliationActionKind.ADOPTED, result.report.kinds())  # type: ignore[union-attr]
        order = tracker.require_order("probex-s1-000001")
        self.assertIs(order.status, OrderStatus.OPEN)
        self.assertAlmostEqual(order.quantity, 0.002)
        self.assertEqual(len(accounting.fills.fills), 0)

    def test_sc2_lost_local_order_present_externally_is_restored(self) -> None:
        recovery, _, tracker, _ = build_recovery(
            responses=recovery_responses(position=_flat_position(), open_orders=[binance_order_payload()])
        )
        local = _open_local(tracker)
        # 本地曾判定 LOST，但外部事实显示订单仍在，且已部分成交
        response = binance_order_payload(status="PARTIALLY_FILLED", executed_qty="0.001", avg_price="60000.0")
        recovery.rest.fetcher.responses["/fapi/v1/openOrders"] = [response]  # type: ignore[attr-defined]
        recovery.rest.fetcher.responses["/fapi/v2/positionRisk"] = _long_position()  # type: ignore[attr-defined]
        tracker.mark_lost(local.client_order_id, timestamp=BASE_TS - 5, reason="ack timeout")

        result = recovery.run(stream_state=stream_state(), snapshot_provider=recovery.fetch_snapshot)

        self.assertIn(ReconciliationActionKind.RESTORED, result.report.kinds())  # type: ignore[union-attr]
        self.assertIs(result.status, RecoveryStatus.RECOVERED)
        order = tracker.require_order(local.client_order_id)
        self.assertIs(order.status, OrderStatus.PARTIALLY_FILLED)
        self.assertAlmostEqual(order.filled_quantity, 0.001)

    def test_sc2_local_terminal_order_is_not_resurrected(self) -> None:
        recovery, _, tracker, _ = build_recovery(
            responses=recovery_responses(position=_flat_position(), open_orders=[binance_order_payload()])
        )
        local = _open_local(tracker)
        _cancel_local(tracker, local.client_order_id)

        result = recovery.run(stream_state=stream_state(), snapshot_provider=recovery.fetch_snapshot)

        self.assertIn(ReconciliationActionKind.STATUS_CONFLICT, result.report.kinds())  # type: ignore[union-attr]
        self.assertIs(result.status, RecoveryStatus.BLOCKED)  # 事实自相矛盾 ⇒ 不得声称 RECOVERED
        self.assertIn(RecoveryReason.RECONCILIATION_NOT_CONVERGED, result.reasons)
        self.assertIs(tracker.require_order(local.client_order_id).status, OrderStatus.CANCELED)

    def test_missing_local_order_is_marked_lost(self) -> None:
        recovery, _, tracker, _ = build_recovery(responses=recovery_responses(position=_flat_position()))
        local = _open_local(tracker)

        result = recovery.run(stream_state=stream_state(), snapshot_provider=recovery.fetch_snapshot)

        self.assertIn(ReconciliationActionKind.MARKED_LOST, result.report.kinds())  # type: ignore[union-attr]
        self.assertIs(tracker.require_order(local.client_order_id).status, OrderStatus.LOST)
        # LOST 是**已知的不确定性**（uncertain exposure，D-021/D-022），不是"未知 ⇒ 不能启动"
        self.assertIs(result.status, RecoveryStatus.RECOVERED)
        self.assertEqual(result.uncertain_orders, (local.client_order_id,))
        self.assertAlmostEqual(tracker.uncertain_exposure(), 0.002 * 60_000.0)
        self.assertEqual(tracker.confirmed_open_exposure(), 0.0)

    def test_history_terminal_status_converges_local_order(self) -> None:
        """外部已不在挂单列表，但历史明确是终态 ⇒ 收敛到终态（不判 LOST）。"""
        recovery, _, tracker, _ = build_recovery(
            responses=recovery_responses(
                position=_flat_position(),
                history=[binance_order_payload(status="FILLED", executed_qty="0.002", avg_price="60000.0")],
            )
        )
        local = _open_local(tracker)
        recovery.rest.fetcher.responses["/fapi/v2/positionRisk"] = _long_position("0.002")  # type: ignore[attr-defined]
        tracker.mark_lost(local.client_order_id, timestamp=BASE_TS - 5, reason="ack timeout")

        result = recovery.run(stream_state=stream_state(), snapshot_provider=recovery.fetch_snapshot)

        self.assertIs(tracker.require_order(local.client_order_id).status, OrderStatus.FILLED)
        self.assertIs(result.status, RecoveryStatus.RECOVERED)

    def test_sc3_missed_fill_is_applied_to_tracker_once(self) -> None:
        """第 1 次恢复只看到未成交挂单；之后交易所出现成交（本地遗漏）⇒ 只补一次。"""
        responses = recovery_responses(
            position=_flat_position(), open_orders=[binance_order_payload()]
        )
        recovery, fetcher, tracker, accounting = build_recovery(responses=responses)

        first = recovery.run(stream_state=stream_state(), snapshot_provider=recovery.fetch_snapshot)
        self.assertEqual(len(first.report.canonical_fills), 0)  # type: ignore[union-attr]
        cid = first.snapshot.open_orders[0].client_order_id  # type: ignore[union-attr]

        # 外部出现了成交（本地从未收到 user stream 事件）
        filled = binance_order_payload(status="PARTIALLY_FILLED", executed_qty="0.001", avg_price="60000.0")
        fetcher.responses["/fapi/v1/openOrders"] = [filled]
        fetcher.responses["/fapi/v1/allOrders"] = [filled]
        fetcher.responses["/fapi/v1/userTrades"] = [binance_trade_payload(timestamp=BASE_TS + 5)]
        fetcher.sequences["/fapi/v2/positionRisk"] = [_long_position(), _long_position()]
        fetcher.responses["/fapi/v2/positionRisk"] = _long_position()

        second = recovery.run(stream_state=stream_state(), snapshot_provider=recovery.fetch_snapshot)
        third = recovery.run(stream_state=stream_state(), snapshot_provider=recovery.fetch_snapshot)

        self.assertEqual(len(second.report.canonical_fills), 1)  # type: ignore[union-attr]
        self.assertEqual(len(third.report.canonical_fills), 0)  # type: ignore[union-attr]
        self.assertAlmostEqual(tracker.require_order(cid).filled_quantity, 0.001)
        self.assertIs(second.status, RecoveryStatus.RECOVERED)
        self.assertIs(third.status, RecoveryStatus.RECOVERED)
        self.assertEqual(len(accounting.fills.fills), 1)  # baseline 之后的成交入账一次

    def test_sc9_second_recovery_is_idempotent(self) -> None:
        responses = recovery_responses(position=_flat_position(), open_orders=[binance_order_payload()])
        recovery, _, tracker, accounting = build_recovery(responses=responses)

        first = recovery.run(stream_state=stream_state(), snapshot_provider=recovery.fetch_snapshot)
        baseline_after_first = accounting.baseline
        second = recovery.run(stream_state=stream_state(), snapshot_provider=recovery.fetch_snapshot)

        self.assertIs(first.status, RecoveryStatus.RECOVERED)
        self.assertIs(second.status, RecoveryStatus.RECOVERED)
        self.assertEqual(second.report.corrective_actions, ())  # type: ignore[union-attr]  # SC-9
        self.assertIs(accounting.baseline, baseline_after_first)  # 没有第二次 bootstrap
        self.assertEqual(len(tracker.orders), 1)

    def test_missed_fill_is_recorded_when_accounting_already_has_state(self) -> None:
        """已 bootstrap 过（第二次启动）⇒ 新的成交必须补进账本，且只补一次。"""
        responses = recovery_responses(position=_flat_position(), open_orders=[binance_order_payload()])
        recovery, fetcher, _, accounting = build_recovery(responses=responses)

        first = recovery.run(stream_state=stream_state(), snapshot_provider=recovery.fetch_snapshot)
        self.assertTrue(first.recovered)
        baseline_captured_at = accounting.baseline.captured_at  # type: ignore[union-attr]

        # baseline 之后成交：必须补进账本（且只补一次）
        filled = binance_order_payload(status="PARTIALLY_FILLED", executed_qty="0.001", avg_price="60000.0")
        fetcher.responses["/fapi/v1/openOrders"] = [filled]
        fetcher.responses["/fapi/v1/allOrders"] = [filled]
        fetcher.responses["/fapi/v1/userTrades"] = [
            binance_trade_payload(timestamp=baseline_captured_at + 5)
        ]
        fetcher.sequences["/fapi/v2/positionRisk"] = [_long_position(), _long_position()]
        fetcher.responses["/fapi/v2/positionRisk"] = _long_position()

        second = recovery.run(stream_state=stream_state(), snapshot_provider=recovery.fetch_snapshot)
        third = recovery.run(stream_state=stream_state(), snapshot_provider=recovery.fetch_snapshot)

        self.assertEqual(len(second.report.canonical_fills), 1)  # type: ignore[union-attr]
        self.assertEqual(len(accounting.fills.fills), 1)  # 入账一次
        self.assertEqual(len(third.report.canonical_fills), 0)  # type: ignore[union-attr]
        self.assertEqual(len(accounting.fills.fills), 1)  # 不重复入账
        self.assertIs(second.status, RecoveryStatus.RECOVERED)
        self.assertAlmostEqual(accounting.position(SYMBOL).qty, 0.001)

    def test_venue_defaults_to_binance(self) -> None:
        recovery, _, tracker, _ = build_recovery(
            responses=recovery_responses(position=_flat_position(), open_orders=[binance_order_payload()])
        )

        recovery.run(stream_state=stream_state(), snapshot_provider=recovery.fetch_snapshot)

        self.assertIs(tracker.venue, Venue.BINANCE)


if __name__ == "__main__":
    unittest.main()
