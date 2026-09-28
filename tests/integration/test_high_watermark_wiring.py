"""P0001.9.4.2 集成：HWM → RiskSnapshot → RiskGate → readiness（SC-15/17/18/19）。

以及"重启后继续同一 epoch"的端到端接线（持久化文件 → 新 tracker → 新 snapshot）。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from portfolio.accounting import AccountingCore
from portfolio.types import ExternalAccountBaseline, Side
from readiness import (
    LiveReadinessGate,
    LiveReadinessReason,
    LiveReadinessStatus,
    ReadinessPolicy,
)
from risk.gate import RiskGate
from risk.high_watermark import HighWatermarkEvidence, HighWatermarkTracker
from risk.limits import RiskLimits
from risk.snapshot import build_risk_snapshot, utc_day_start_ms
from risk.types import OrderProposal, RiskDecisionType
from storage.high_watermark import JsonHighWatermarkStore
from tests.readiness_support import activated_tracker, satisfied_preconditions
from tests.support import BASE_TS, SYMBOL, make_fill

POLICY = ReadinessPolicy(
    max_clock_uncertainty_ms=1_000,
    max_median_private_lag_ms=5_000,
    max_calibration_age_ms=3_600_000,
    max_available_balance_age_ms=60_000,
)


def _proposal(*, quantity: float = 0.1) -> OrderProposal:
    return OrderProposal(symbol=SYMBOL, side=Side.BUY, quantity=quantity, price=100.0)


def _accounting(*, balance: float = 1_000.0, mark: float = 100.0) -> AccountingCore:
    core = AccountingCore(initial_balance=balance)
    core.update_mark_price(SYMBOL, mark, timestamp=BASE_TS)
    return core


class SnapshotWiringTest(unittest.TestCase):
    def test_sc18_high_watermark_evidence_produces_peak_and_drawdown(self) -> None:
        tracker, _store = activated_tracker(equity=1_000.0)
        tracker.observe_equity(current_equity=1_200.0, ts=BASE_TS + 1)
        accounting = _accounting(balance=1_000.0)
        accounting.record_fill(make_fill("f1", Side.BUY, 100.0, 1.0, fee=200.0))  # 手续费 200 ⇒ equity 800

        snapshot = build_risk_snapshot(
            accounting, symbol=SYMBOL, now_ms=BASE_TS + 2, high_watermark=tracker.evidence()
        )

        self.assertIsNotNone(snapshot.peak_equity)
        self.assertIsNotNone(snapshot.drawdown_pct)
        self.assertEqual(snapshot.peak_equity, 1_200.0)
        self.assertAlmostEqual(snapshot.drawdown or 0.0, 400.0)
        self.assertAlmostEqual(snapshot.drawdown_pct or 0.0, 400.0 / 1_200.0)

    def test_uninitialized_high_watermark_keeps_drawdown_unknown(self) -> None:
        accounting = _accounting()

        snapshot = build_risk_snapshot(
            accounting, symbol=SYMBOL, now_ms=BASE_TS, high_watermark=HighWatermarkEvidence.uninitialized()
        )

        self.assertIsNone(snapshot.peak_equity)
        self.assertIsNone(snapshot.drawdown)
        self.assertIsNone(snapshot.drawdown_pct)

    def test_sc17_paper_path_is_unchanged(self) -> None:
        """SC-17：不传 HWM ⇒ 保持原 session-derived 峰值语义。"""
        accounting = _accounting(balance=1_000.0)
        accounting.record_fill(make_fill("f1", Side.BUY, 100.0, 1.0, fee=10.0))

        baseline = build_risk_snapshot(accounting, symbol=SYMBOL, now_ms=BASE_TS + 1)
        explicit_none = build_risk_snapshot(
            accounting, symbol=SYMBOL, now_ms=BASE_TS + 1, high_watermark=None
        )

        self.assertEqual(baseline, explicit_none)
        self.assertEqual(baseline.peak_equity, accounting.peak_equity)


class RiskGateDrawdownTest(unittest.TestCase):
    def test_sc19_drawdown_limit_is_enforced_not_missing(self) -> None:
        tracker, _store = activated_tracker(equity=1_000.0)
        accounting = _accounting(balance=1_000.0)
        accounting.record_fill(make_fill("f1", Side.BUY, 100.0, 1.0, fee=150.0))  # equity 850 ⇒ dd 15%
        snapshot = build_risk_snapshot(
            accounting, symbol=SYMBOL, now_ms=BASE_TS + 1, high_watermark=tracker.evidence()
        )
        gate = RiskGate(RiskLimits(max_drawdown_pct=0.10, max_position_qty=10.0))

        decision = gate.evaluate(_proposal(), snapshot)

        self.assertIs(decision.decision, RiskDecisionType.REJECT)
        self.assertEqual(decision.reason_code.value, "DRAWDOWN_LIMIT")  # 不是 MISSING_DRAWDOWN

    def test_within_limit_allows(self) -> None:
        tracker, _store = activated_tracker(equity=1_000.0)
        accounting = _accounting(balance=1_000.0)
        accounting.record_fill(make_fill("f1", Side.BUY, 100.0, 1.0, fee=10.0))  # dd 1%
        snapshot = build_risk_snapshot(
            accounting, symbol=SYMBOL, now_ms=BASE_TS + 1, high_watermark=tracker.evidence()
        )

        decision = RiskGate(RiskLimits(max_drawdown_pct=0.10, max_position_qty=10.0)).evaluate(
            _proposal(), snapshot
        )

        self.assertIs(decision.decision, RiskDecisionType.ALLOW)

    def test_without_high_watermark_gate_fails_closed(self) -> None:
        accounting = _accounting(balance=1_000.0)
        accounting.record_fill(make_fill("f1", Side.BUY, 100.0, 1.0, fee=10.0))
        snapshot = build_risk_snapshot(
            accounting, symbol=SYMBOL, now_ms=BASE_TS + 1, high_watermark=HighWatermarkEvidence.uninitialized()
        )

        decision = RiskGate(RiskLimits(max_drawdown_pct=0.10, max_position_qty=10.0)).evaluate(
            _proposal(), snapshot
        )

        self.assertEqual(decision.reason_code.value, "MISSING_DRAWDOWN")


class RestartContinuityTest(unittest.TestCase):
    def test_restart_continues_epoch_and_keeps_drawdown(self) -> None:
        with TemporaryDirectory() as tmp:
            store = JsonHighWatermarkStore(Path(tmp) / "hwm.json")
            first = HighWatermarkTracker(state=None, store=store)
            first.activate(
                preconditions=satisfied_preconditions(), current_equity=1_000.0, activation_id="act-1", ts=BASE_TS
            )
            first.observe_equity(current_equity=1_500.0, ts=BASE_TS + 1)

            # 进程重启：新的 tracker 从磁盘加载
            restarted = HighWatermarkTracker(state=store.load(), store=store)
            accounting = _accounting(balance=1_000.0)
            accounting.record_fill(make_fill("f1", Side.BUY, 100.0, 1.0, fee=300.0))  # equity 700
            snapshot = build_risk_snapshot(
                accounting, symbol=SYMBOL, now_ms=BASE_TS + 2, high_watermark=restarted.evidence()
            )

            self.assertEqual(snapshot.peak_equity, 1_500.0)  # 未被重启洗掉
            self.assertAlmostEqual(snapshot.drawdown or 0.0, 800.0)
            self.assertEqual(restarted.state.activation_id, "act-1")  # 同一 epoch


class ReadinessDrawdownTest(unittest.TestCase):
    """SC-16 / SC-18 / SC-19：HWM 有效 ⇒ readiness 不再报 `HISTORICAL_DRAWDOWN_UNKNOWN`。"""

    def _evidence(self, snapshot, hwm):
        import time

        from connectors.binance.private.auth import ClockCalibration
        from connectors.binance.private.recovery import RecoveryStatus
        from readiness import (
            AccountEvidence,
            Environment,
            HistoricalRiskEvidence,
            LiveReadinessEvidence,
            LiveRiskPolicy,
            PrivateStreamEvidence,
        )
        from tests.readiness_support import environment_evidence, market_evidence
        from risk.types import KillSwitchMode

        now = int(time.time() * 1000)
        return LiveReadinessEvidence(
            now_ms=now,
            recovery_status=RecoveryStatus.RECOVERED,
            private_stream=PrivateStreamEvidence(
                listen_key_state="ACTIVE",
                continuity_assumed=True,
                boundary_present=True,
                median_private_lag_ms=50,
                clock_calibration=ClockCalibration(offset_ms=0, round_trip_ms=20, uncertainty_ms=10, measured_at_ms=now),
            ),
            account=AccountEvidence(can_trade=True, available_balance=1_000.0, available_balance_captured_at=now),
            historical_risk=HistoricalRiskEvidence(
                daily_pnl_known=True,
                drawdown_known=snapshot.drawdown is not None,
                peak_equity_known=snapshot.peak_equity is not None,
            ),
            environment=environment_evidence(environment=Environment.TESTNET),
            market=market_evidence(),
            risk_policy=LiveRiskPolicy(
                max_position_qty=1.0,
                max_order_notional=1_000.0,
                max_open_order_exposure=1_000.0,
                max_daily_loss=100.0,
                max_drawdown_pct=0.2,
                max_leverage=3.0,
                max_mark_age_ms=5_000,
                kill_switch_mode=KillSwitchMode.NORMAL,
            ),
            high_watermark=hwm,
        )

    def test_valid_high_watermark_lifts_drawdown_blocker(self) -> None:
        tracker, _store = activated_tracker(equity=1_000.0)
        accounting = _accounting(balance=1_000.0)
        snapshot = build_risk_snapshot(
            accounting, symbol=SYMBOL, now_ms=BASE_TS + 1, high_watermark=tracker.evidence()
        )

        result = LiveReadinessGate(policy=POLICY).evaluate(self._evidence(snapshot, tracker.evidence()))

        self.assertNotIn(LiveReadinessReason.HISTORICAL_DRAWDOWN_UNKNOWN, result.reasons)
        self.assertIs(result.status, LiveReadinessStatus.LIVE_READY)

    def test_uninitialized_high_watermark_blocks_readiness(self) -> None:
        accounting = _accounting(balance=1_000.0)
        hwm = HighWatermarkEvidence.uninitialized()
        snapshot = build_risk_snapshot(accounting, symbol=SYMBOL, now_ms=BASE_TS + 1, high_watermark=hwm)

        result = LiveReadinessGate(policy=POLICY).evaluate(self._evidence(snapshot, hwm))

        self.assertIn(LiveReadinessReason.HIGH_WATERMARK_NOT_INITIALIZED, result.reasons)
        self.assertIn(LiveReadinessReason.HISTORICAL_DRAWDOWN_UNKNOWN, result.reasons)
        self.assertIs(result.status, LiveReadinessStatus.BLOCKED)


class BaselineInteractionTest(unittest.TestCase):
    """与 P0001.9.4.1 的 daily PnL 一致：两者互不覆盖。"""

    def test_high_watermark_and_historical_baseline_coexist(self) -> None:
        from risk.history import HistoricalRiskBaseline
        from portfolio.types import ExternalAccountBaseline

        tracker, _store = activated_tracker(equity=5_000.0)
        accounting = AccountingCore(initial_balance=0.0)
        accounting.bootstrap_from_baseline(
            ExternalAccountBaseline(
                symbol=SYMBOL,
                wallet_balance=5_000.0,
                available_balance=5_000.0,
                position_qty=0.0,
                entry_price=0.0,
                mark_price=0.0,
                liquidation_price=0.0,
                captured_at=BASE_TS,
            )
        )
        baseline = HistoricalRiskBaseline(
            day_start_ts=BASE_TS - 1,
            cutoff_ts=BASE_TS,
            daily_net_realized=-12.5,
            trading_rows=3,
            non_trading_rows=0,
            coverage_complete=True,
        )
        now = BASE_TS + 10

        snapshot = build_risk_snapshot(
            accounting,
            symbol=SYMBOL,
            now_ms=now,
            day_start_ts=utc_day_start_ms(now),
            historical_baseline=baseline,
            high_watermark=tracker.evidence(),
        )

        self.assertAlmostEqual(snapshot.realized_pnl_today or 0.0, -12.5)  # 来自 income baseline
        self.assertEqual(snapshot.peak_equity, 5_000.0)  # 来自 durable HWM
        self.assertEqual(snapshot.drawdown, 0.0)


if __name__ == "__main__":
    unittest.main()
