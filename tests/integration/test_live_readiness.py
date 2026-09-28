"""P0001.9.4 集成测试：把 private runtime / recovery / accounting 的真实事实组装成 readiness 判定。

覆盖：

- SC-1：`RECOVERED = true` + 历史风险 UNKNOWN ⇒ `BLOCKED`（real 代码路径，不是手工构造布尔值）；
- SC-4：live 可用余额来自交易所 account snapshot，进入 `RiskSnapshot` 的是 `BINANCE_ACCOUNT_SNAPSHOT`；
- SC-5：Paper/Replay 路径仍用本地推导（来源 = `LOCAL_DERIVED`）；
- SC-11：`evaluate()` 是纯判定，不产生任何 REST 调用。
"""

from __future__ import annotations

import unittest

from connectors.binance.private.account import parse_account_snapshot
from connectors.binance.private.recovery import RecoveryStatus
from market.events.types import Milliseconds
from portfolio.accounting import AccountingCore
from readiness import (
    Environment,
    EnvironmentEvidence,
    LiveReadinessEvidence,
    LiveReadinessGate,
    LiveReadinessReason,
    LiveReadinessScope,
    LiveReadinessStatus,
    LiveRiskPolicy,
    ReadinessPolicy,
    account_evidence,
    environment_evidence,
    exchange_available_balance,
    historical_risk_evidence_from_snapshot,
    private_stream_evidence,
)
from risk.snapshot import build_risk_snapshot, utc_day_start_ms
from risk.types import AvailableBalanceSource
from tests.readiness_support import active_hwm, activated_tracker, environment_evidence, market_evidence
from tests.private_support import (
    SYMBOL,
    account_payload,
    build_recovery,
    build_runtime,
    order_update_message,
    position_risk_payload,
    recovery_responses,
    stream_state,
)
from tests.support import BASE_TS

#: 测试用阈值（**不是**业务决策值：真实数值由人类配置后经 ReadinessPolicy 注入）。
READINESS_POLICY = ReadinessPolicy(
    max_clock_uncertainty_ms=100,
    max_median_private_lag_ms=2_000,
    max_calibration_age_ms=600_000,
    max_available_balance_age_ms=30_000,
)
LIVE_RISK_POLICY = LiveRiskPolicy(
    max_position_qty=0.01,
    max_order_notional=500.0,
    max_open_order_exposure=500.0,
    max_daily_loss=50.0,
    max_drawdown_pct=0.10,
    max_leverage=3.0,
    max_mark_age_ms=5_000,
)


def _flat() -> list[dict]:
    return position_risk_payload(position_amt="0", entry_price="0", mark_price="0")


def _evidence(
    *,
    runtime,
    recovery_status: RecoveryStatus,
    accounting: AccountingCore,
    now_ms: Milliseconds,
    day_start_ts: Milliseconds,
    mainnet_validated: bool = False,
    environment: Environment = Environment.TESTNET,
    market_ready: bool = True,
    high_watermark=None,
    use_high_watermark_in_snapshot: bool = False,
) -> LiveReadinessEvidence:
    """用**真实对象**组装证据（telemetry / account snapshot / accounting → RiskSnapshot）。"""
    observation = parse_account_snapshot(
        account_payload(), symbol=SYMBOL, receive_ts=now_ms, process_ts=now_ms
    )
    hwm = active_hwm() if high_watermark is None else high_watermark
    snapshot = build_risk_snapshot(
        accounting,
        symbol=SYMBOL,
        now_ms=now_ms,
        day_start_ts=day_start_ts,
        exchange_available_balance=exchange_available_balance(observation),
        high_watermark=hwm if use_high_watermark_in_snapshot else None,
    )
    return LiveReadinessEvidence(
        now_ms=now_ms,
        recovery_status=recovery_status,
        private_stream=private_stream_evidence(
            runtime.telemetry, boundary_present=runtime.snapshot_boundary is not None
        ),
        account=account_evidence(observation),
        historical_risk=historical_risk_evidence_from_snapshot(snapshot),
        environment=environment_evidence(environment=environment, validated=mainnet_validated),
        market=market_evidence(ready=market_ready),
        risk_policy=LIVE_RISK_POLICY,
        high_watermark=hwm,
    ), snapshot


class RecoveryToReadinessTest(unittest.TestCase):
    def _stack(self):
        runtime, fetcher, factory = build_runtime()
        recovery, _, tracker, accounting = build_recovery(
            responses=recovery_responses(position=_flat()), clock=lambda: BASE_TS
        )
        recovery.bind(runtime)
        runtime.start()
        result = recovery.run(stream_state=stream_state(), snapshot_provider=recovery.fetch_snapshot)
        self.assertIs(result.status, RecoveryStatus.RECOVERED)
        return runtime, fetcher, factory, recovery, tracker, accounting

    def test_sc1_recovered_account_with_unknown_history_is_blocked(self) -> None:
        runtime, _fetcher, _factory, recovery, _tracker, accounting = self._stack()
        gate = LiveReadinessGate(policy=READINESS_POLICY)

        evidence, snapshot = _evidence(
            runtime=runtime,
            recovery_status=recovery.state,
            accounting=accounting,
            now_ms=BASE_TS,
            day_start_ts=BASE_TS - 1,  # 日界早于 baseline ⇒ 当日 PnL 未知
        )
        result = gate.evaluate(evidence)

        self.assertIs(result.status, LiveReadinessStatus.BLOCKED)  # SC-1：这是正确结果
        self.assertIsNone(result.scope)
        self.assertIn(LiveReadinessReason.HISTORICAL_DAILY_PNL_UNKNOWN, result.reasons)
        self.assertIn(LiveReadinessReason.HISTORICAL_DRAWDOWN_UNKNOWN, result.reasons)
        self.assertIsNone(snapshot.peak_equity)
        self.assertIsNone(snapshot.drawdown_pct)

    def test_sc4_exchange_available_balance_reaches_risk_snapshot(self) -> None:
        runtime, _fetcher, _factory, recovery, _tracker, accounting = self._stack()

        _evidence_, snapshot = _evidence(
            runtime=runtime,
            recovery_status=recovery.state,
            accounting=accounting,
            now_ms=BASE_TS,
            day_start_ts=BASE_TS - 1,
        )

        # 交易所事实（fixture 的 availableBalance = 900.25）优先于本地推导（balance=1000.50）
        self.assertEqual(snapshot.available_balance, 900.25)
        self.assertIs(snapshot.available_balance_source, AvailableBalanceSource.BINANCE_ACCOUNT_SNAPSHOT)
        self.assertEqual(snapshot.available_balance_age_ms, 0)

    def test_sc5_paper_path_keeps_local_derivation(self) -> None:
        runtime, _fetcher, _factory, recovery, _tracker, accounting = self._stack()

        snapshot = build_risk_snapshot(accounting, symbol=SYMBOL, now_ms=BASE_TS, open_order_exposure=10.0)

        self.assertIs(snapshot.available_balance_source, AvailableBalanceSource.LOCAL_DERIVED)
        self.assertEqual(snapshot.available_balance, accounting.balance - 10.0)
        self.assertIsNotNone(runtime.telemetry)  # 与 private 侧无关，语义未被改写

    def test_ready_path_when_history_is_known_and_stream_is_live(self) -> None:
        runtime, _fetcher, factory, recovery, _tracker, _accounting = self._stack()
        # 一个有历史观测的正常会话（非 baseline 恢复）：峰值与当日 PnL 都已知
        known = AccountingCore(initial_balance=1_000.0)
        known.update_mark_price(SYMBOL, 100.0, timestamp=BASE_TS - 5)
        factory.connection.push(order_update_message(event_ts=BASE_TS - 40))
        runtime.pump_once(timeout_s=0.01)

        now = BASE_TS
        tracker, _store = activated_tracker(equity=known.equity() or 0.0, ts=now)
        evidence, snapshot = _evidence(
            runtime=runtime,
            recovery_status=recovery.state,
            accounting=known,
            now_ms=now,
            day_start_ts=utc_day_start_ms(now),
            high_watermark=tracker.evidence(),
            use_high_watermark_in_snapshot=True,  # SC-18 / SC-19
        )
        result = LiveReadinessGate(policy=READINESS_POLICY).evaluate(evidence)

        self.assertIsNotNone(snapshot.peak_equity)
        self.assertIs(result.status, LiveReadinessStatus.LIVE_READY)
        self.assertIs(result.scope, LiveReadinessScope.TESTNET_LIVE_READY)

    def test_sc11_gate_makes_no_rest_calls(self) -> None:
        runtime, fetcher, _factory, recovery, _tracker, accounting = self._stack()
        evidence, _snapshot = _evidence(
            runtime=runtime,
            recovery_status=recovery.state,
            accounting=accounting,
            now_ms=BASE_TS,
            day_start_ts=BASE_TS - 1,
        )
        calls_before = list(fetcher.calls)

        LiveReadinessGate(policy=READINESS_POLICY).evaluate(evidence)

        self.assertEqual(fetcher.calls, calls_before)

    def test_disconnect_blocks_readiness_through_the_real_wiring(self) -> None:
        runtime, _fetcher, factory, recovery, _tracker, accounting = self._stack()

        factory.connection.drop()
        runtime.pump_once(timeout_s=0.01)

        evidence, _snapshot = _evidence(
            runtime=runtime,
            recovery_status=recovery.state,
            accounting=accounting,
            now_ms=BASE_TS,
            day_start_ts=BASE_TS - 1,
        )
        result = LiveReadinessGate(policy=READINESS_POLICY).evaluate(evidence)

        self.assertIn(LiveReadinessReason.RECOVERY_NOT_READY, result.reasons)
        self.assertIn(LiveReadinessReason.PRIVATE_STREAM_NOT_READY, result.reasons)

    def test_mainnet_evidence_is_not_transferred_from_testnet_validation(self) -> None:
        runtime, _fetcher, _factory, recovery, _tracker, accounting = self._stack()

        evidence, _snapshot = _evidence(
            runtime=runtime,
            recovery_status=recovery.state,
            accounting=accounting,
            now_ms=BASE_TS,
            day_start_ts=BASE_TS - 1,
            environment=Environment.MAINNET,
        )
        result = LiveReadinessGate(policy=READINESS_POLICY).evaluate(evidence)

        self.assertIn(LiveReadinessReason.MAINNET_PRIVATE_NOT_VALIDATED, result.reasons)
        self.assertIsNone(result.scope)


class EnvironmentEvidenceTest(unittest.TestCase):
    def test_mainnet_validation_must_be_typed_and_complete(self) -> None:
        """P0001.9.5 §4 / SC-3：主网验收不再由裸 bool 表达。"""
        from readiness import (
            EnvironmentValidationEvidence,
            EnvironmentValidationStatus,
            historical_risk_evidence,
        )

        with self.assertRaises(Exception):
            environment_evidence(environment="mainnet")  # type: ignore[arg-type]
        with self.assertRaises(Exception):
            historical_risk_evidence(daily_pnl_known="yes", drawdown_known=True, peak_equity_known=True)  # type: ignore[arg-type]

        not_validated = EnvironmentEvidence.not_validated(environment=Environment.MAINNET)
        self.assertFalse(not_validated.mainnet_private_validated)

        # 状态 VALIDATED 但证据不完整（缺 validation_id / account_scope / evidence_source）⇒ 仍不算 validated
        incomplete = EnvironmentValidationEvidence(
            environment=Environment.MAINNET,
            validation_status=EnvironmentValidationStatus.VALIDATED,
            validated_at=BASE_TS,
        )
        self.assertFalse(incomplete.mainnet_private_validated)

    def test_testnet_validation_never_counts_as_mainnet(self) -> None:
        from readiness import EnvironmentValidationEvidence, EnvironmentValidationStatus

        testnet_record = EnvironmentValidationEvidence(
            environment=Environment.TESTNET,
            validation_status=EnvironmentValidationStatus.VALIDATED,
            validated_at=BASE_TS,
            validation_id="testnet-validation",
            account_scope="binance-usdm-testnet",
            evidence_source="live-runner",
        )

        self.assertFalse(testnet_record.mainnet_private_validated)


if __name__ == "__main__":
    unittest.main()
