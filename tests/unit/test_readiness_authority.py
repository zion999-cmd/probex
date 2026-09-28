"""P0001.9.5 单元测试：`ExecutionReadinessAuthority` 签发与校验（SC-5 – SC-16 / SC-18）。"""

from __future__ import annotations

import unittest

from market.readiness import MarketReadinessPolicy
from readiness import (
    AccountEvidence,
    AuthorityError,
    AuthorityInvalidReason,
    Environment,
    EnvironmentEvidence,
    ExecutionReadinessAuthority,
    ExecutionReadinessAuthorityValidator,
    HistoricalRiskEvidence,
    LiveReadinessEvidence,
    LiveReadinessGate,
    LiveReadinessReason,
    LiveReadinessScope,
    LiveReadinessStatus,
    LiveRiskPolicy,
    PrivateStreamEvidence,
    ReadinessPolicy,
    ReadinessProvenance,
    RecoveryGeneration,
    evidence_digest,
    issue_authority,
    risk_policy_fingerprint,
)
from risk.high_watermark import HighWatermarkEvidence
from risk.types import KillSwitchMode
from tests.readiness_support import active_hwm, environment_evidence, market_evidence
from tests.support import BASE_TS

NOW = BASE_TS + 10_000
POLICY = ReadinessPolicy(
    max_clock_uncertainty_ms=1_000,
    max_median_private_lag_ms=5_000,
    max_calibration_age_ms=3_600_000,
    max_available_balance_age_ms=60_000,
)
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


def ready_evidence(**overrides: object) -> LiveReadinessEvidence:
    from connectors.binance.private.auth import ClockCalibration
    from connectors.binance.private.recovery import RecoveryStatus

    values: dict[str, object] = {
        "now_ms": NOW,
        "recovery_status": RecoveryStatus.RECOVERED,
        "private_stream": PrivateStreamEvidence(
            listen_key_state="ACTIVE",
            continuity_assumed=True,
            boundary_present=True,
            median_private_lag_ms=50,
            clock_calibration=ClockCalibration(offset_ms=0, round_trip_ms=20, uncertainty_ms=10, measured_at_ms=NOW),
        ),
        "account": AccountEvidence(can_trade=True, available_balance=1_000.0, available_balance_captured_at=NOW),
        "historical_risk": HistoricalRiskEvidence(daily_pnl_known=True, drawdown_known=True, peak_equity_known=True),
        "environment": environment_evidence(environment=Environment.TESTNET),
        "market": market_evidence(),
        "risk_policy": RISK_POLICY,
        "high_watermark": active_hwm(),
    }
    values.update(overrides)
    return LiveReadinessEvidence(**values)  # type: ignore[arg-type]


def provenance(**overrides: object) -> ReadinessProvenance:
    values: dict[str, object] = {
        "recovery_generation": RecoveryGeneration(discontinuity_count=0, invalidation_count=0),
        "market_generation": 3,
        "market_evidence_ts": NOW - 100,
        "account_snapshot_ts": NOW - 50,
        "clock_calibration_ts": NOW - 20,
        "hwm_activation_id": "act-1",
        "hwm_generation": 1,
        "risk_policy_fingerprint": risk_policy_fingerprint(RISK_POLICY.to_limits()),
        "evidence_digest": evidence_digest({"a": 1, "b": [1, 2]}),
    }
    values.update(overrides)
    return ReadinessProvenance(**values)  # type: ignore[arg-type]


def issue(**overrides: object) -> ExecutionReadinessAuthority:
    values: dict[str, object] = {
        "result": LiveReadinessGate(policy=POLICY).evaluate(ready_evidence()),
        "provenance": provenance(),
        "authority_id": "auth-1",
        "now_ms": NOW,
        "authority_ttl_ms": 5_000,
        "environment": Environment.TESTNET,
    }
    values.update(overrides)
    return issue_authority(**values)  # type: ignore[arg-type]


def validate(authority: ExecutionReadinessAuthority, **overrides: object):
    values: dict[str, object] = {
        "now_ms": NOW + 1,
        "requested_environment": Environment.TESTNET,
        "recovery_generation": RecoveryGeneration(discontinuity_count=0, invalidation_count=0),
        "market_generation": 3,
        "hwm_activation_id": "act-1",
        "hwm_generation": 1,
        "kill_switch_mode": KillSwitchMode.NORMAL,
    }
    values.update(overrides)
    return ExecutionReadinessAuthorityValidator().validate(authority, **values)  # type: ignore[arg-type]


class IssueAuthorityTest(unittest.TestCase):
    def test_sc5_live_ready_issues_authority_with_versions(self) -> None:
        authority = issue()

        self.assertIs(authority.readiness_status, LiveReadinessStatus.LIVE_READY)
        self.assertIs(authority.scope, LiveReadinessScope.TESTNET_LIVE_READY)
        self.assertEqual(authority.issued_at_ms, NOW)
        self.assertEqual(authority.expires_at_ms, NOW + 5_000)
        self.assertEqual(authority.recovery_generation, RecoveryGeneration(0, 0))
        self.assertEqual(authority.market_generation, 3)
        self.assertEqual(authority.hwm_activation_id, "act-1")
        self.assertEqual(authority.hwm_generation, 1)
        self.assertTrue(authority.evidence_digest.startswith("sha256:"))
        self.assertTrue(authority.risk_policy_fingerprint)

    def test_sc6_blocked_result_never_issues_authority(self) -> None:
        blocked = LiveReadinessGate(policy=POLICY).evaluate(
            ready_evidence(high_watermark=HighWatermarkEvidence.uninitialized())
        )

        self.assertIs(blocked.status, LiveReadinessStatus.BLOCKED)
        with self.assertRaises(AuthorityError):
            issue(result=blocked)

    def test_ttl_must_be_explicit_and_positive(self) -> None:
        for ttl in (0, -1, True, 1.5):
            with self.subTest(ttl=ttl):
                with self.assertRaises(AuthorityError):
                    issue(authority_ttl_ms=ttl)

    def test_scope_must_match_environment(self) -> None:
        # readiness 结果是 TESTNET，但调用方声明 MAINNET ⇒ 拒绝签发
        with self.assertRaises(AuthorityError):
            issue(environment=Environment.MAINNET)

    def test_authority_only_accepts_live_ready_status(self) -> None:
        with self.assertRaises(AuthorityError):
            ExecutionReadinessAuthority(
                authority_id="x",
                issued_at_ms=NOW,
                expires_at_ms=NOW + 1,
                scope=LiveReadinessScope.TESTNET_LIVE_READY,
                environment=Environment.TESTNET,
                recovery_generation=RecoveryGeneration(0, 0),
                market_generation=1,
                market_evidence_ts=NOW,
                account_snapshot_ts=None,
                clock_calibration_ts=None,
                hwm_activation_id=None,
                hwm_generation=0,
                risk_policy_fingerprint=None,
                evidence_digest="sha256:x",
                readiness_status=LiveReadinessStatus.BLOCKED,
            )

    def test_sc18_authority_has_no_execution_capability(self) -> None:
        """授权对象只有"事实 + 版本"，不含任何 submit/cancel/下单方法。"""
        authority = issue()

        for forbidden in ("submit", "cancel", "place_order", "to_request", "execute"):
            with self.subTest(name=forbidden):
                self.assertFalse(hasattr(authority, forbidden))


class ValidateAuthorityTest(unittest.TestCase):
    def test_valid_within_ttl(self) -> None:
        verdict = validate(issue())

        self.assertTrue(verdict.valid)
        self.assertEqual(verdict.reasons, ())

    def test_sc9_expired_authority_is_rejected(self) -> None:
        verdict = validate(issue(), now_ms=NOW + 5_001)

        self.assertFalse(verdict.valid)
        self.assertIn(AuthorityInvalidReason.AUTHORITY_EXPIRED, verdict.reasons)

    def test_ttl_boundary_is_inclusive(self) -> None:
        self.assertTrue(validate(issue(), now_ms=NOW + 5_000).valid)

    def test_sc8_testnet_authority_cannot_be_used_for_mainnet(self) -> None:
        verdict = validate(issue(), requested_environment=Environment.MAINNET)

        self.assertFalse(verdict.valid)
        self.assertIn(AuthorityInvalidReason.SCOPE_MISMATCH, verdict.reasons)

    def test_sc11_recovery_generation_change_invalidates(self) -> None:
        for generation in (
            RecoveryGeneration(discontinuity_count=1, invalidation_count=0),
            RecoveryGeneration(discontinuity_count=0, invalidation_count=1),
        ):
            with self.subTest(generation=generation):
                verdict = validate(issue(), recovery_generation=generation)
                self.assertFalse(verdict.valid)
                self.assertIn(AuthorityInvalidReason.RECOVERY_GENERATION_CHANGED, verdict.reasons)

    def test_sc12_market_generation_change_invalidates(self) -> None:
        verdict = validate(issue(), market_generation=4)

        self.assertFalse(verdict.valid)
        self.assertIn(AuthorityInvalidReason.MARKET_GENERATION_CHANGED, verdict.reasons)

    def test_sc13_high_watermark_change_invalidates(self) -> None:
        for overrides in (
            {"hwm_activation_id": "act-2"},
            {"hwm_generation": 2},
            {"hwm_activation_id": None, "hwm_generation": 0},
        ):
            with self.subTest(overrides=overrides):
                verdict = validate(issue(), **overrides)
                self.assertFalse(verdict.valid)
                self.assertIn(AuthorityInvalidReason.HIGH_WATERMARK_CHANGED, verdict.reasons)

    def test_sc14_kill_switch_change_invalidates(self) -> None:
        for mode in (KillSwitchMode.REDUCE_ONLY, KillSwitchMode.HALT_ALL):
            with self.subTest(mode=mode):
                verdict = validate(issue(), kill_switch_mode=mode)
                self.assertFalse(verdict.valid)
                self.assertIn(AuthorityInvalidReason.KILL_SWITCH_NOT_NORMAL, verdict.reasons)

    def test_multiple_problems_are_all_reported(self) -> None:
        verdict = validate(
            issue(), now_ms=NOW + 10_000, market_generation=9, kill_switch_mode=KillSwitchMode.HALT_ALL
        )

        self.assertEqual(
            set(verdict.reasons),
            {
                AuthorityInvalidReason.AUTHORITY_EXPIRED,
                AuthorityInvalidReason.MARKET_GENERATION_CHANGED,
                AuthorityInvalidReason.KILL_SWITCH_NOT_NORMAL,
            },
        )
        self.assertEqual(len(verdict.details), 3)

    def test_validator_requires_typed_inputs(self) -> None:
        authority = issue()
        for overrides in (
            {"now_ms": "now"},
            {"requested_environment": "mainnet"},
            {"recovery_generation": (0, 0)},
            {"kill_switch_mode": "NORMAL"},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(AuthorityError):
                    validate(authority, **overrides)
        with self.assertRaises(AuthorityError):
            ExecutionReadinessAuthorityValidator().validate("authority", **{  # type: ignore[arg-type]
                "now_ms": NOW,
                "requested_environment": Environment.TESTNET,
                "recovery_generation": RecoveryGeneration(0, 0),
                "market_generation": 1,
                "hwm_activation_id": None,
                "hwm_generation": 0,
                "kill_switch_mode": KillSwitchMode.NORMAL,
            })


class DigestTest(unittest.TestCase):
    def test_sc15_same_facts_produce_same_digest(self) -> None:
        payload = {"a": 1, "b": [1, 2], "c": {"d": "x"}}

        self.assertEqual(evidence_digest(payload), evidence_digest(dict(reversed(list(payload.items())))))

    def test_sc16_changed_facts_change_digest(self) -> None:
        base = {"market_generation": 3, "hwm_generation": 1}

        self.assertNotEqual(evidence_digest(base), evidence_digest({**base, "market_generation": 4}))
        self.assertNotEqual(evidence_digest(base), evidence_digest({**base, "hwm_generation": 2}))

    def test_policy_fingerprint_tracks_limits(self) -> None:
        base = risk_policy_fingerprint(RISK_POLICY.to_limits())

        self.assertNotEqual(base, risk_policy_fingerprint(RISK_POLICY.to_limits().__class__(max_daily_loss=1.0)))
        with self.assertRaises(AuthorityError):
            risk_policy_fingerprint(None)


class EvidenceContractTest(unittest.TestCase):
    def test_market_evidence_is_required_and_typed(self) -> None:
        with self.assertRaises(Exception):
            ready_evidence(market=True)  # 裸 bool 不再被接受

    def test_environment_validation_must_be_typed(self) -> None:
        with self.assertRaises(Exception):
            ready_evidence(environment=Environment.TESTNET)  # 旧式裸环境对象不再被接受

    def test_mainnet_without_validation_is_blocked(self) -> None:
        result = LiveReadinessGate(policy=POLICY).evaluate(
            ready_evidence(environment=environment_evidence(environment=Environment.MAINNET))
        )

        self.assertIn(LiveReadinessReason.MAINNET_PRIVATE_NOT_VALIDATED, result.reasons)

    def test_market_policy_is_explicit(self) -> None:
        with self.assertRaises(TypeError):
            MarketReadinessPolicy()  # type: ignore[call-arg]


if __name__ == "__main__":
    unittest.main()
