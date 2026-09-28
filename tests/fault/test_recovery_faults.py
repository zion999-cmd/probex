"""P0001.9.3 Fault：恢复门在不确定 / 冲突 / 失败时**必须** BLOCKED（SC-4 / SC-5 / SC-7 / SC-8）。"""

from __future__ import annotations

import unittest

from execution.reconciliation import ReconciliationActionKind
from execution.types import OrderStatus
from portfolio.accounting import AccountingCore
from portfolio.types import Side
from risk.types import OrderProposal

from connectors.binance.private.recovery import RecoveryReason, RecoveryStatus, StreamState
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


class ForeignOwnershipFaultTest(unittest.TestCase):
    def test_sc4_foreign_open_order_blocks_recovery(self) -> None:
        responses = recovery_responses(
            position=_flat_position(),
            open_orders=[
                binance_order_payload(client_order_id="probex-s1-000001"),
                binance_order_payload(order_id=900, client_order_id="manual-order"),
            ],
        )
        recovery, _, tracker, accounting = build_recovery(responses=responses)

        result = recovery.run(stream_state=stream_state(), snapshot_provider=recovery.fetch_snapshot)

        self.assertIs(result.status, RecoveryStatus.BLOCKED)
        self.assertEqual(result.reasons, (RecoveryReason.FOREIGN_OPEN_ORDER,))
        self.assertEqual(len(tracker.orders), 0)  # 不 ignore、不 adopt
        self.assertFalse(accounting.baseline_applied)  # 未建立 baseline

    def test_foreign_history_is_ignored(self) -> None:
        responses = recovery_responses(
            position=_flat_position(),
            history=[binance_order_payload(order_id=901, client_order_id="bot-1", status="CANCELED")],
        )
        recovery, _, tracker, _ = build_recovery(responses=responses)

        result = recovery.run(stream_state=stream_state(), snapshot_provider=recovery.fetch_snapshot)

        self.assertIs(result.status, RecoveryStatus.RECOVERED)
        self.assertEqual(len(tracker.orders), 0)


class UnknownExposureFaultTest(unittest.TestCase):
    def test_sc5_unresolved_order_blocks_recovery(self) -> None:
        recovery, _, tracker, accounting = build_recovery(
            responses=recovery_responses(position=_flat_position())
        )
        tracker.create(OrderProposal(symbol=SYMBOL, side=Side.BUY, quantity=0.002, price=60_000.0),
                       timestamp=BASE_TS - 10)
        tracker.note_unresolved_order("probex-s1-000001", reason="ack unknown")

        result = recovery.run(stream_state=stream_state(), snapshot_provider=recovery.fetch_snapshot)

        self.assertIs(result.status, RecoveryStatus.BLOCKED)
        self.assertEqual(result.reasons, (RecoveryReason.UNRESOLVED_ORDERS,))
        self.assertTrue(tracker.has_unknown_exposure)
        self.assertFalse(accounting.baseline_applied)

    def test_sc5_unadoptable_external_order_is_not_treated_as_zero(self) -> None:
        """外部订单缺 side/price 资料 ⇒ 不 adopt（不按 0），记录为不确定暴露 ⇒ BLOCKED。"""
        responses = recovery_responses(position=_flat_position())
        recovery, fetcher, tracker, _ = build_recovery(responses=responses)
        snapshot = recovery.fetch_snapshot()
        from dataclasses import replace

        from execution.types import ExternalOrder

        blind = ExternalOrder(
            client_order_id="probex-s1-000001", exchange_order_id="101", symbol=SYMBOL,
            side=None, price=None, quantity=0.002, status=OrderStatus.OPEN,
            filled_quantity=0.0, avg_fill_price=0.0,
        )
        tampered = replace(snapshot, open_orders=(blind,))

        calls_before = len(fetcher.calls)

        result = recovery.run(stream_state=stream_state(), snapshot=tampered)

        self.assertIs(result.status, RecoveryStatus.BLOCKED)
        self.assertIn(RecoveryReason.UNRESOLVED_ORDERS, result.reasons)
        self.assertIn(ReconciliationActionKind.ADOPT_REJECTED, result.report.kinds())  # type: ignore[union-attr]
        self.assertTrue(tracker.has_unknown_exposure)
        self.assertEqual(tracker.confirmed_open_exposure(), 0.0)
        self.assertEqual(len(fetcher.calls), calls_before)  # 注入 snapshot 时不再发 REST 请求


class TerminalConflictFaultTest(unittest.TestCase):
    def test_local_canceled_vs_history_filled_blocks(self) -> None:
        """两侧终态不一致（本地 CANCELED / 交易所 FILLED）⇒ BLOCKED，不崩溃、不静默接受。"""
        from execution.events import OrderAccepted, OrderCanceled
        from risk.types import OrderProposal

        responses = recovery_responses(
            position=_flat_position(),
            history=[binance_order_payload(status="FILLED", executed_qty="0.002", avg_price="60000.0")],
        )
        recovery, _, tracker, _ = build_recovery(responses=responses)
        order = tracker.create(
            OrderProposal(symbol=SYMBOL, side=Side.BUY, quantity=0.002, price=60_000.0), timestamp=BASE_TS - 20
        )
        tracker.on_event(
            OrderAccepted(client_order_id=order.client_order_id, timestamp=BASE_TS - 19, exchange_order_id="101")
        )
        tracker.on_event(OrderCanceled(client_order_id=order.client_order_id, timestamp=BASE_TS - 5))

        result = recovery.run(stream_state=stream_state(), snapshot_provider=recovery.fetch_snapshot)

        self.assertIs(result.status, RecoveryStatus.BLOCKED)
        self.assertIn(RecoveryReason.RECONCILIATION_NOT_CONVERGED, result.reasons)
        self.assertIn(ReconciliationActionKind.STATUS_CONFLICT, result.report.kinds())  # type: ignore[union-attr]
        self.assertIs(tracker.require_order(order.client_order_id).status, OrderStatus.CANCELED)  # 未被回退

    def test_local_filled_matching_history_filled_is_idempotent(self) -> None:
        from execution.events import FillReceived, OrderAccepted

        responses = recovery_responses(
            position=_long_position_placeholder(),
            history=[binance_order_payload(status="FILLED", executed_qty="0.002", avg_price="60000.0")],
        )
        recovery, _, tracker, _ = build_recovery(responses=responses)
        order = tracker.create(
            OrderProposal(symbol=SYMBOL, side=Side.BUY, quantity=0.002, price=60_000.0), timestamp=BASE_TS - 20
        )
        tracker.on_event(
            OrderAccepted(client_order_id=order.client_order_id, timestamp=BASE_TS - 19, exchange_order_id="101")
        )
        tracker.on_event(
            FillReceived(
                client_order_id=order.client_order_id, execution_id="e1", price=60_000.0, quantity=0.002,
                timestamp=BASE_TS - 10, trade_id="t1",
            )
        )

        result = recovery.run(stream_state=stream_state(), snapshot_provider=recovery.fetch_snapshot)

        self.assertIs(result.status, RecoveryStatus.RECOVERED)
        self.assertNotIn(ReconciliationActionKind.STATUS_CONFLICT, result.report.kinds())  # type: ignore[union-attr]


class StreamStateFaultTest(unittest.TestCase):
    def _reasons(self, state: StreamState) -> tuple[RecoveryReason, ...]:
        recovery, _, _, _ = build_recovery(responses=recovery_responses(position=_flat_position(),
                                                                        open_orders=[binance_order_payload()]))
        return recovery.run(stream_state=state, snapshot_provider=recovery.fetch_snapshot).reasons

    def test_not_active_stream_blocks(self) -> None:
        self.assertEqual(self._reasons(stream_state(active=False)), (RecoveryReason.STREAM_NOT_ACTIVE,))

    def test_missing_continuity_blocks(self) -> None:
        self.assertEqual(self._reasons(stream_state(continuity=False)),
                         (RecoveryReason.CONTINUITY_NOT_ASSUMED,))

    def test_missing_boundary_blocks(self) -> None:
        self.assertEqual(self._reasons(stream_state(boundary=False)), (RecoveryReason.BOUNDARY_MISSING,))

    def test_all_missing_reasons_are_reported(self) -> None:
        reasons = self._reasons(stream_state(active=False, continuity=False, boundary=False))

        self.assertEqual(
            set(reasons),
            {RecoveryReason.STREAM_NOT_ACTIVE, RecoveryReason.CONTINUITY_NOT_ASSUMED,
             RecoveryReason.BOUNDARY_MISSING},
        )

    def test_sc8_reconnect_invalidates_recovery_state(self) -> None:
        recovery, _, _, _ = build_recovery(responses=recovery_responses(position=_flat_position()))
        first = recovery.run(stream_state=stream_state(), snapshot_provider=recovery.fetch_snapshot)
        self.assertIs(first.status, RecoveryStatus.RECOVERED)

        recovery.invalidate(reason="user stream disconnected")
        self.assertIs(recovery.state, RecoveryStatus.NOT_RECOVERED)

        # 自动重连后（stream 重新 ACTIVE）如果跳过 recovery 流程，状态不得自动回到 RECOVERED
        self.assertIs(recovery.state, RecoveryStatus.NOT_RECOVERED)
        again = recovery.run(stream_state=stream_state(), snapshot_provider=recovery.fetch_snapshot)
        self.assertIs(again.status, RecoveryStatus.RECOVERED)  # 重新走完整流程后才允许
        self.assertEqual(len(recovery.reason_log), 1)

    def test_snapshot_read_failures_map_to_reason_codes(self) -> None:
        cases = {
            "/fapi/v2/account": RecoveryReason.ACCOUNT_SNAPSHOT_FAILED,
            "/fapi/v2/positionRisk": RecoveryReason.POSITION_SNAPSHOT_FAILED,
            "/fapi/v1/openOrders": RecoveryReason.OPEN_ORDERS_READ_FAILED,
            "/fapi/v1/allOrders": RecoveryReason.ORDER_HISTORY_READ_FAILED,
            "/fapi/v1/userTrades": RecoveryReason.FILLS_READ_FAILED,
        }
        for path, expected in cases.items():
            recovery, fetcher, _, accounting = build_recovery(
                responses=recovery_responses(position=_flat_position())
            )
            fetcher.failures[path] = 1
            with self.subTest(path=path):
                result = recovery.run(stream_state=stream_state(), snapshot_provider=recovery.fetch_snapshot)
                self.assertIs(result.status, RecoveryStatus.BLOCKED)
                self.assertEqual(result.reasons, (expected,))
                self.assertFalse(accounting.baseline_applied)


class AccountingBaselineFaultTest(unittest.TestCase):
    def test_sc7_existing_session_fills_refuse_bootstrap_and_block(self) -> None:
        accounting = AccountingCore(initial_balance=1_000.0)
        accounting.update_mark_price(SYMBOL, 60_000.0, timestamp=BASE_TS)
        accounting.record_fill(
            __import__("tests.support", fromlist=["make_fill"]).make_fill("f1", Side.BUY, 60_000.0, 0.001)
        )
        recovery, _, _, _ = build_recovery(responses=recovery_responses(), accounting=accounting)

        result = recovery.run(stream_state=stream_state(), snapshot_provider=recovery.fetch_snapshot)

        self.assertIs(result.status, RecoveryStatus.BLOCKED)
        self.assertEqual(result.reasons, (RecoveryReason.BASELINE_ALREADY_APPLIED,))
        self.assertIsNone(accounting.baseline)

    def test_position_mismatch_after_recovery_blocks(self) -> None:
        """两次读取之间仓位发生变化（竞态）⇒ 不得声称 RECOVERED。"""
        recovery, fetcher, _, _ = build_recovery(responses=recovery_responses())
        fetcher.sequences["/fapi/v2/positionRisk"] = [
            recovery_responses()["/fapi/v2/positionRisk"],  # baseline 读取
            _flat_position(),  # 复核读取：与 baseline 不一致
        ]

        result = recovery.run(stream_state=stream_state(), snapshot_provider=recovery.fetch_snapshot)

        self.assertIs(result.status, RecoveryStatus.BLOCKED)
        self.assertEqual(result.reasons, (RecoveryReason.BASELINE_MISMATCH,))

    def test_hedge_mode_is_blocked(self) -> None:
        recovery, _, _, _ = build_recovery(
            responses=recovery_responses(
                position=position_risk_payload(position_side="LONG"), position_amt_default=None
            ) if False else recovery_responses()
        )
        # 直接注入一个非 one-way 的 snapshot（解析层已先 fail closed，此处覆盖注入路径）
        snapshot = recovery.fetch_snapshot()
        hedged = type(snapshot)(
            **{**{f: getattr(snapshot, f) for f in snapshot.__slots__},
               "position": _hedge_position(snapshot.position)}
        )

        result = recovery.run(stream_state=stream_state(), snapshot=hedged)

        self.assertIs(result.status, RecoveryStatus.BLOCKED)
        self.assertEqual(result.reasons, (RecoveryReason.ACCOUNT_MODE_UNSUPPORTED,))

    def test_symbol_contract_violation_is_blocked(self) -> None:
        recovery, _, _, _ = build_recovery(responses=recovery_responses(position=_flat_position()))
        snapshot = recovery.fetch_snapshot()
        foreign_order = binance_order_payload(symbol="ETHUSDT")
        from connectors.binance.private.orders import parse_external_orders

        tampered = type(snapshot)(
            **{**{f: getattr(snapshot, f) for f in snapshot.__slots__},
               "open_orders": parse_external_orders([foreign_order], symbol="ETHUSDT")}
        )

        result = recovery.run(stream_state=stream_state(), snapshot=tampered)

        self.assertIs(result.status, RecoveryStatus.BLOCKED)
        self.assertEqual(result.reasons, (RecoveryReason.SYMBOL_CONTRACT_VIOLATION,))

    def test_requires_snapshot_or_provider(self) -> None:
        from connectors.binance.private.errors import PrivateFormatError

        recovery, _, _, _ = build_recovery(responses=recovery_responses(position=_flat_position()))
        with self.assertRaises(PrivateFormatError):
            recovery.run(stream_state=stream_state())

    def test_clock_must_be_injected(self) -> None:
        from connectors.binance.private.errors import PrivateFormatError
        from connectors.binance.private.recovery import StartupRecovery

        recovery, _, _, _ = build_recovery()
        with self.assertRaises(PrivateFormatError):
            StartupRecovery(
                rest=recovery.rest, tracker=recovery.tracker, accounting=recovery.accounting, symbol=SYMBOL
            )


def _long_position_placeholder() -> list[dict]:
    return position_risk_payload(position_amt="0.002", entry_price="60000", mark_price="60000")


def _hedge_position(position):
    from dataclasses import replace

    return replace(position, position_side="LONG")


if __name__ == "__main__":
    unittest.main()
