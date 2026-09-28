"""P0001.9.5 集成：受控 collector → Gate → Authority → Validator 全链。

覆盖：

- SC-1（正式路径不再接受裸 `market_ready=True`）；
- SC-2/3/4（typed market / environment validation / HWM 只能来自 tracker）；
- SC-10/11/12/13（discontinuity、recovery generation、market generation、HWM rebase 让旧授权失效）。

使用真实 `PrivateAccountRuntime`（假 REST/WS）+ 真实 `StartupRecovery` + 真实 `HighWatermarkTracker`。
"""

from __future__ import annotations

import unittest

from connectors.binance.private.recovery import RecoveryStatus
from market.readiness import MarketReadinessPolicy
from portfolio.types import Side
from readiness import (
    AuthorityInvalidReason,
    Environment,
    ExecutionReadinessAuthorityValidator,
    LiveReadinessEvidence,
    LiveReadinessGate,
    LiveReadinessReason,
    LiveReadinessStatus,
    LiveRiskPolicy,
    ReadinessEvidenceCollector,
    ReadinessPolicy,
    issue_authority,
)
from readiness import AuthorityError, recovery_generation
from risk.high_watermark import HighWatermarkTracker
from risk.types import KillSwitchMode
from tests.private_support import (
    SYMBOL,
    build_recovery,
    build_runtime,
    order_update_message,
    recovery_responses,
    stream_state,
)
from tests.readiness_support import (
    InMemoryHighWatermarkStore,
    environment_evidence,
    market_evidence,
    satisfied_preconditions,
)
from tests.support import BASE_TS, make_fill

READINESS_POLICY = ReadinessPolicy(
    max_clock_uncertainty_ms=1_000,
    max_median_private_lag_ms=5_000,
    max_calibration_age_ms=3_600_000,
    max_available_balance_age_ms=60_000,
)
MARKET_POLICY = MarketReadinessPolicy(max_mark_age_ms=5_000, max_feed_age_ms=5_000)
RISK_POLICY = LiveRiskPolicy(
    max_position_qty=1.0,
    max_order_notional=1_000.0,
    max_open_order_exposure=1_000.0,
    max_daily_loss=100.0,
    max_drawdown_pct=0.2,
    max_leverage=3.0,
    max_mark_age_ms=5_000,
    kill_switch_mode=KillSwitchMode.NORMAL,
)


class CollectorStack:
    """一次性搭好：runtime + recovery + accounting + HWM tracker + collector。"""

    def __init__(self, *, market=None, environment=None) -> None:
        self.runtime, self.fetcher, self.factory = build_runtime()
        self.recovery, _fetcher, self.tracker_orders, self.accounting = build_recovery(
            responses=recovery_responses(), clock=lambda: BASE_TS
        )
        self.recovery.bind(self.runtime)
        self.runtime.start()
        self.recovery.run(stream_state=stream_state(), snapshot_provider=self.recovery.fetch_snapshot)
        # 制造一条真实业务事件样本（fake WS）⇒ corrected private latency 已知（否则 readiness 会因 unknown 阻塞）
        self.factory.connection.push(order_update_message(event_ts=BASE_TS - 40))
        self.runtime.pump_once(timeout_s=0.01)
        self.store = InMemoryHighWatermarkStore()
        self.hwm = HighWatermarkTracker(state=None, store=self.store)
        self.hwm.activate(
            preconditions=satisfied_preconditions(), current_equity=1_000.0, activation_id="act-1", ts=BASE_TS
        )
        self.market = market if market is not None else market_evidence()
        self.collector = ReadinessEvidenceCollector.from_high_watermark_tracker(
            self.hwm,
            runtime=self.runtime,
            recovery=self.recovery,
            market=self.market,
            environment=environment if environment is not None else environment_evidence(),
            risk_policy=RISK_POLICY,
        )

    def baseline(self):
        """受信历史 baseline（cutoff == accounting baseline 水位线 ⇒ 本地段已知 = 0）。"""
        from risk.history import HistoricalRiskBaseline

        return HistoricalRiskBaseline(
            day_start_ts=BASE_TS - 1,
            cutoff_ts=BASE_TS,
            daily_net_realized=-1.25,
            trading_rows=3,
            non_trading_rows=1,
            coverage_complete=True,
        )

    def collect(self, *, now_ms: int = BASE_TS + 10, with_baseline: bool = True):
        return self.collector.collect(
            accounting=self.accounting,
            now_ms=now_ms,
            day_start_ts=BASE_TS - 1,
            historical_baseline=self.baseline() if with_baseline else None,
        )

    def validator_kwargs(self, *, now_ms: int) -> dict:
        generation = recovery_generation(runtime=self.runtime, recovery=self.recovery)
        evidence = self.hwm.evidence()
        return {
            "now_ms": now_ms,
            "requested_environment": Environment.TESTNET,
            "recovery_generation": generation,
            "market_generation": self.market.generation,
            "hwm_activation_id": evidence.activation_id,
            "hwm_generation": evidence.generation,
            "kill_switch_mode": KillSwitchMode.NORMAL,
        }


class FullChainTest(unittest.TestCase):
    def test_collector_reads_real_runtime_facts(self) -> None:
        stack = CollectorStack()
        collected = stack.collect()

        evidence = collected.evidence
        self.assertEqual(evidence.private_stream.listen_key_state, "ACTIVE")
        self.assertTrue(evidence.private_stream.boundary_present)
        self.assertEqual(evidence.account.available_balance, 900.25)
        self.assertIsNotNone(evidence.private_stream.clock_calibration)
        self.assertEqual(collected.provenance.hwm_activation_id, "act-1")
        self.assertEqual(collected.provenance.recovery_generation.discontinuity_count, 0)
        self.assertTrue(collected.provenance.evidence_digest.startswith("sha256:"))

    def test_evidence_digest_is_stable_and_tracks_facts(self) -> None:
        stack = CollectorStack()
        first = stack.collect().provenance.evidence_digest
        second = stack.collect().provenance.evidence_digest

        self.assertEqual(first, second)
        stack.hwm.observe_equity(current_equity=1_500.0, ts=BASE_TS + 5)
        third = stack.collect().provenance.evidence_digest

        self.assertNotEqual(first, third)

    def test_bare_market_bool_is_rejected_by_contract(self) -> None:
        stack = CollectorStack()
        collected = stack.collect()

        with self.assertRaises(Exception):
            LiveReadinessEvidence(
                now_ms=BASE_TS,
                recovery_status=RecoveryStatus.RECOVERED,
                private_stream=collected.evidence.private_stream,
                account=collected.evidence.account,
                historical_risk=collected.evidence.historical_risk,
                environment=environment_evidence(),
                market=True,  # 裸 bool 不再被接受（SC-1）
                risk_policy=RISK_POLICY,
            )

    def test_market_not_ready_blocks_and_never_issues_authority(self) -> None:
        stack = CollectorStack(market=market_evidence(ready=False))
        collected = stack.collect()
        result = LiveReadinessGate(policy=READINESS_POLICY).evaluate(collected.evidence)

        self.assertIn(LiveReadinessReason.MARKET_NOT_READY, result.reasons)
        with self.assertRaises(AuthorityError):
            issue_authority(
                result,
                provenance=collected.provenance,
                authority_id="auth-x",
                now_ms=BASE_TS + 10,
                authority_ttl_ms=5_000,
                environment=Environment.TESTNET,
            )

    def test_mainnet_without_validation_stays_blocked(self) -> None:
        stack = CollectorStack(environment=environment_evidence(environment=Environment.MAINNET))
        collected = stack.collect()

        result = LiveReadinessGate(policy=READINESS_POLICY).evaluate(collected.evidence)

        self.assertIn(LiveReadinessReason.MAINNET_PRIVATE_NOT_VALIDATED, result.reasons)
        self.assertIs(result.status, LiveReadinessStatus.BLOCKED)


class InvalidationTest(unittest.TestCase):
    """SC-10/11/12/13：一旦 runtime / HWM epoch 变化，旧授权立即失效。"""

    def _issued(self):
        stack = CollectorStack()
        collected = stack.collect()
        self.assertTrue(collected.evidence.historical_risk.daily_pnl_known, "stack must be LIVE_READY-able")
        result = LiveReadinessGate(policy=READINESS_POLICY).evaluate(collected.evidence)
        authority = issue_authority(
            result,
            provenance=collected.provenance,
            authority_id="auth-1",
            now_ms=BASE_TS + 10,
            authority_ttl_ms=60_000,
            environment=Environment.TESTNET,
        )
        return stack, authority

    def test_sc10_discontinuity_invalidates_authority(self) -> None:
        stack, authority = self._issued()

        stack.factory.connection.drop()
        stack.runtime.pump_once(timeout_s=0.01)  # 真实断线路径 ⇒ recovery 自动失效 + generation 变化

        verdict = ExecutionReadinessAuthorityValidator().validate(
            authority, **stack.validator_kwargs(now_ms=BASE_TS + 20)
        )

        self.assertFalse(verdict.valid)
        self.assertIn(AuthorityInvalidReason.RECOVERY_GENERATION_CHANGED, verdict.reasons)

    def test_sc11_explicit_invalidation_changes_generation(self) -> None:
        stack, authority = self._issued()

        stack.recovery.invalidate(reason="operator invalidation")

        verdict = ExecutionReadinessAuthorityValidator().validate(
            authority, **stack.validator_kwargs(now_ms=BASE_TS + 20)
        )

        self.assertFalse(verdict.valid)
        self.assertIn(AuthorityInvalidReason.RECOVERY_GENERATION_CHANGED, verdict.reasons)

    def test_sc12_market_generation_change_invalidates_authority(self) -> None:
        stack, authority = self._issued()

        verdict = ExecutionReadinessAuthorityValidator().validate(
            authority,
            now_ms=BASE_TS + 20,
            requested_environment=Environment.TESTNET,
            recovery_generation=authority.recovery_generation,
            market_generation=authority.market_generation + 1,
            hwm_activation_id=authority.hwm_activation_id,
            hwm_generation=authority.hwm_generation,
            kill_switch_mode=KillSwitchMode.NORMAL,
        )

        self.assertFalse(verdict.valid)
        self.assertIn(AuthorityInvalidReason.MARKET_GENERATION_CHANGED, verdict.reasons)

    def test_sc13_hwm_rebase_invalidates_authority(self) -> None:
        stack, authority = self._issued()

        # 真实流程：先因外部资金流失效（INVALIDATED），再由人类显式 rebase 到新 epoch
        stack.hwm.invalidate_for_capital_flow(reason="TRANSFER detected", ts=BASE_TS + 14)
        stack.hwm.rebase(
            preconditions=satisfied_preconditions(),
            current_equity=900.0,
            activation_id="act-2",
            ts=BASE_TS + 15,
            reason="explicit human rebase",
        )

        verdict = ExecutionReadinessAuthorityValidator().validate(
            authority, **stack.validator_kwargs(now_ms=BASE_TS + 20)
        )

        self.assertFalse(verdict.valid)
        self.assertIn(AuthorityInvalidReason.HIGH_WATERMARK_CHANGED, verdict.reasons)

    def test_sc14_kill_switch_change_invalidates_authority(self) -> None:
        stack, authority = self._issued()

        verdict = ExecutionReadinessAuthorityValidator().validate(
            authority,
            now_ms=BASE_TS + 20,
            requested_environment=Environment.TESTNET,
            recovery_generation=authority.recovery_generation,
            market_generation=authority.market_generation,
            hwm_activation_id=authority.hwm_activation_id,
            hwm_generation=authority.hwm_generation,
            kill_switch_mode=KillSwitchMode.HALT_ALL,
        )

        self.assertFalse(verdict.valid)
        self.assertIn(AuthorityInvalidReason.KILL_SWITCH_NOT_NORMAL, verdict.reasons)


class AccountingInteractionTest(unittest.TestCase):
    def test_drawdown_flows_into_collected_evidence(self) -> None:
        stack = CollectorStack()
        stack.hwm.observe_equity(current_equity=1_200.0, ts=BASE_TS + 1)
        stack.accounting.record_fill(make_fill("f1", Side.BUY, 100.0, 1.0, fee=200.0))  # equity 800

        collected = stack.collect()

        self.assertTrue(collected.evidence.historical_risk.drawdown_known)
        self.assertEqual(collected.provenance.hwm_activation_id, "act-1")
        self.assertEqual(collected.provenance.hwm_generation, 1)
        self.assertEqual(SYMBOL, "BTCUSDT")


if __name__ == "__main__":
    unittest.main()
