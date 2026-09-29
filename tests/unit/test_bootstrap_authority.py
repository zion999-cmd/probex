"""P0001.9.7.1 cold-start BOOTSTRAP authority 单测（人类验收顺序 1 – 8）。"""

from __future__ import annotations

import unittest

from connectors.binance.private.recovery import RecoveryStatus
from risk.types import KillSwitchMode
from readiness import (
    Environment,
    LiveReadinessReason,
    LiveReadinessResult,
    LiveReadinessScope,
    LiveReadinessStatus,
    PrivateLatencyStatus,
    RecoveryGeneration,
)
from readiness.authority import (
    AuthorityError,
    AuthorityInvalidReason,
    ExecutionReadinessAuthority,
    ReadinessProvenance,
    issue_authority,
)
from readiness.bootstrap import (
    AuthorityKind,
    BOOTSTRAP_MAX_ORDERS,
    BOOTSTRAP_MAX_NOTIONAL_USDT,
    BootstrapAuthority,
    BootstrapAuthorityCoordinator,
    BootstrapEligibility,
    BootstrapPhase,
    issue_bootstrap_authority,
)

NOW = 1_700_000_000_000
TTL = 30_000


def provenance(*, market_generation: int = 7) -> ReadinessProvenance:
    return ReadinessProvenance(
        recovery_generation=RecoveryGeneration(0, 1),
        market_generation=market_generation,
        market_evidence_ts=NOW - 1_000,
        account_snapshot_ts=NOW - 1_500,
        clock_calibration_ts=NOW - 2_000,
        hwm_activation_id="act-1",
        hwm_generation=2,
        risk_policy_fingerprint="risk:abc",
        evidence_digest="sha256:deadbeef",
    )


def unobserved_result() -> LiveReadinessResult:
    return LiveReadinessResult(
        status=LiveReadinessStatus.BOOTSTRAP_ELIGIBLE,
        scope=None,
        reasons=(LiveReadinessReason.PRIVATE_LATENCY_UNOBSERVED,),
        details=("no private business events observed yet (cold start: no sample exists)",),
        latency_status=PrivateLatencyStatus.UNOBSERVED,
    )


def live_ready_result(latency: PrivateLatencyStatus) -> LiveReadinessResult:
    status = (
        LiveReadinessStatus.LIVE_READY
        if latency is PrivateLatencyStatus.HEALTHY
        else LiveReadinessStatus.BLOCKED
    )
    reasons: tuple[LiveReadinessReason, ...] = ()
    scope = None
    if latency is PrivateLatencyStatus.UNHEALTHY:
        reasons = (LiveReadinessReason.PRIVATE_LATENCY_TOO_HIGH,)
    elif latency is PrivateLatencyStatus.UNKNOWN:
        reasons = (LiveReadinessReason.PRIVATE_LATENCY_UNKNOWN,)
    elif latency is PrivateLatencyStatus.HEALTHY:
        scope = LiveReadinessScope.TESTNET_LIVE_READY
    return LiveReadinessResult(
        status=status,
        scope=scope,
        reasons=reasons,
        latency_status=latency,
    )


def eligibility(**overrides: object) -> BootstrapEligibility:
    base: dict[str, object] = {
        "recovery_status": RecoveryStatus.RECOVERED,
        "market_ready": True,
        "private_continuity_valid": True,
        "account_flat": True,
        "probex_open_orders": 0,
        "foreign_open_orders": 0,
        "uncertain_exposure_qty": 0.0,
        "latency_status": PrivateLatencyStatus.UNOBSERVED,
        "environment": Environment.TESTNET,
    }
    base.update(overrides)
    return BootstrapEligibility(**base)  # type: ignore[arg-type]


def coordinator() -> BootstrapAuthorityCoordinator:
    return BootstrapAuthorityCoordinator(symbol="BTCUSDT", bootstrap_ttl_ms=TTL, normal_ttl_ms=60_000)


class BootstrapIssuanceTest(unittest.TestCase):
    def test_1_cold_start_can_issue_bootstrap(self) -> None:
        coord = coordinator()
        activation = coord.activate(result=unobserved_result(), eligibility=eligibility(),
                                   provenance=provenance(), now_ms=NOW, environment=Environment.TESTNET)

        self.assertIs(activation.phase, BootstrapPhase.ACTIVE)
        authority = activation.authority
        assert isinstance(authority, BootstrapAuthority)
        self.assertIs(authority.kind, AuthorityKind.BOOTSTRAP)
        self.assertIs(authority.environment, Environment.TESTNET)
        self.assertIs(authority.symbol, "BTCUSDT")
        self.assertEqual(authority.max_orders, BOOTSTRAP_MAX_ORDERS)
        self.assertIs(authority.post_only_required, True)
        self.assertEqual(authority.max_notional_usdt, BOOTSTRAP_MAX_NOTIONAL_USDT)
        self.assertEqual(authority.expires_at_ms, NOW + TTL)
        self.assertEqual(authority.evidence_digest, "sha256:deadbeef")
        self.assertEqual(authority.bootstrap_generation, 1)
        self.assertIs(coord.phase, BootstrapPhase.ACTIVE)
        self.assertIs(coord.bootstrap_authority, authority)
        self.assertEqual(coord.bootstrap_orders_remaining, 1)

    def test_2_any_extra_blocker_forbids_bootstrap(self) -> None:
        cases = {
            "recovery not RECOVERED": {"recovery_status": RecoveryStatus.BLOCKED},
            "market not ready": {"market_ready": False},
            "continuity invalid": {"private_continuity_valid": False},
            "not flat": {"account_flat": False},
            "probex orders != 0": {"probex_open_orders": 1},
            "foreign orders != 0": {"foreign_open_orders": 2},
            "uncertain exposure != 0": {"uncertain_exposure_qty": 0.001},
            "latency UNKNOWN": {"latency_status": PrivateLatencyStatus.UNKNOWN},
            "mainnet": {"environment": Environment.MAINNET},
        }
        for label, override in cases.items():
            with self.subTest(case=label):
                coord = coordinator()
                activation = coord.activate(result=unobserved_result(), eligibility=eligibility(**override),
                                            provenance=provenance(), now_ms=NOW,
                                            environment=Environment.TESTNET)
                self.assertIs(activation.phase, BootstrapPhase.BLOCKED)
                self.assertIsNone(activation.authority)
                self.assertTrue(activation.reasons)

    def test_2b_blocked_readiness_never_bootstraps(self) -> None:
        blocked = LiveReadinessResult(
            status=LiveReadinessStatus.BLOCKED,
            scope=None,
            reasons=(LiveReadinessReason.RECOVERY_NOT_READY, LiveReadinessReason.PRIVATE_LATENCY_UNOBSERVED),
            latency_status=PrivateLatencyStatus.UNOBSERVED,
        )
        coord = coordinator()
        activation = coord.activate(result=blocked, eligibility=eligibility(), provenance=provenance(),
                                    now_ms=NOW, environment=Environment.TESTNET)
        self.assertIs(activation.phase, BootstrapPhase.BLOCKED)
        self.assertIn(LiveReadinessReason.RECOVERY_NOT_READY.value, activation.reasons)

    def test_8_mainnet_can_never_bootstrap(self) -> None:
        with self.assertRaises(AuthorityError):
            issue_bootstrap_authority(unobserved_result(), eligibility=eligibility(),
                                      provenance=provenance(), authority_id="b-1",
                                      bootstrap_generation=1, symbol="BTCUSDT", now_ms=NOW,
                                      authority_ttl_ms=TTL, environment=Environment.MAINNET,
                                      max_notional_usdt=50.0)
        with self.assertRaises(AuthorityError):
            BootstrapAuthority(
                authority_id="b-1", bootstrap_generation=1, issued_at_ms=NOW, expires_at_ms=NOW + TTL,
                environment=Environment.MAINNET, symbol="BTCUSDT", max_orders=1, post_only_required=True,
                max_notional_usdt=50.0, recovery_generation=RecoveryGeneration(0, 1), market_generation=1,
                market_evidence_ts=NOW, evidence_digest="sha256:x",
                readiness_status=LiveReadinessStatus.BOOTSTRAP_ELIGIBLE,
            )

    def test_normal_authority_never_accepts_unobserved(self) -> None:
        with self.assertRaises(AuthorityError):
            issue_authority(unobserved_result(), provenance=provenance(), authority_id="n-1",
                            now_ms=NOW, authority_ttl_ms=TTL, environment=Environment.TESTNET)
        with self.assertRaises(AuthorityError):
            issue_authority(
                LiveReadinessResult(status=LiveReadinessStatus.LIVE_READY, scope=None, reasons=(),
                                    latency_status=PrivateLatencyStatus.UNOBSERVED),
                provenance=provenance(), authority_id="n-1", now_ms=NOW, authority_ttl_ms=TTL,
                environment=Environment.TESTNET,
            )


class BootstrapConsumptionTest(unittest.TestCase):
    def activated(self) -> BootstrapAuthorityCoordinator:
        coord = coordinator()
        activation = coord.activate(result=unobserved_result(), eligibility=eligibility(),
                                   provenance=provenance(), now_ms=NOW, environment=Environment.TESTNET)
        self.assertIs(activation.phase, BootstrapPhase.ACTIVE)
        return coord

    def validate(self, coord: BootstrapAuthorityCoordinator, *, latency: PrivateLatencyStatus,
                 attempts: int | None = None):
        return coord.validator.validate_bootstrap(
            coord.bootstrap_authority, now_ms=NOW + 1_000, requested_environment=Environment.TESTNET,
            symbol="BTCUSDT",
            write_attempts_used=coord.write_attempts if attempts is None else attempts,
            latency_status=latency, recovery_generation=RecoveryGeneration(0, 1), market_generation=7,
            kill_switch_mode=KillSwitchMode.NORMAL,
        )

    def test_3_first_submit_attempt_exhausts_max_orders(self) -> None:
        coord = self.activated()
        verdict = self.validate(coord, latency=PrivateLatencyStatus.UNOBSERVED)
        self.assertTrue(verdict.valid)

        self.assertEqual(coord.record_write_attempt(), 1)
        self.assertEqual(coord.bootstrap_orders_remaining, 0)
        after = self.validate(coord, latency=PrivateLatencyStatus.UNOBSERVED)
        self.assertFalse(after.valid)
        self.assertIn(AuthorityInvalidReason.ORDERS_USED_EXCEEDS_MAX, after.reasons)

    def test_4_unknown_outcome_still_consumes_the_single_attempt(self) -> None:
        """write attempt 计数与结果无关：UNKNOWN 之后**不能**再写入第二笔。"""
        coord = self.activated()
        coord.record_write_attempt()  # 结果未知（UNKNOWN）——调用方不等待 acceptance
        verdict = self.validate(coord, latency=PrivateLatencyStatus.UNOBSERVED)
        self.assertFalse(verdict.valid)
        self.assertIn(AuthorityInvalidReason.ORDERS_USED_EXCEEDS_MAX, verdict.reasons)

    def test_5_private_event_supersedes_bootstrap(self) -> None:
        coord = self.activated()
        after_event = self.validate(coord, latency=PrivateLatencyStatus.HEALTHY)
        self.assertFalse(after_event.valid)
        self.assertIn(AuthorityInvalidReason.BOOTSTRAP_SUPERSEDED, after_event.reasons)

    def test_bootstrap_invalidates_on_generation_change_and_kill_switch(self) -> None:
        coord = self.activated()
        changed = coord.validator.validate_bootstrap(
            coord.bootstrap_authority, now_ms=NOW + 1_000, requested_environment=Environment.TESTNET,
            symbol="BTCUSDT", write_attempts_used=0, latency_status=PrivateLatencyStatus.UNOBSERVED,
            recovery_generation=RecoveryGeneration(1, 1), market_generation=8,
            kill_switch_mode=KillSwitchMode.REDUCE_ONLY,
        )
        self.assertFalse(changed.valid)
        self.assertIn(AuthorityInvalidReason.RECOVERY_GENERATION_CHANGED, changed.reasons)
        self.assertIn(AuthorityInvalidReason.MARKET_GENERATION_CHANGED, changed.reasons)
        self.assertIn(AuthorityInvalidReason.KILL_SWITCH_NOT_NORMAL, changed.reasons)

    def test_bootstrap_scope_and_expiry(self) -> None:
        coord = self.activated()
        other_symbol = coord.validator.validate_bootstrap(
            coord.bootstrap_authority, now_ms=NOW + 1_000, requested_environment=Environment.TESTNET,
            symbol="ETHUSDT", write_attempts_used=0, latency_status=PrivateLatencyStatus.UNOBSERVED,
            recovery_generation=RecoveryGeneration(0, 1), market_generation=7,
            kill_switch_mode=KillSwitchMode.NORMAL,
        )
        self.assertIn(AuthorityInvalidReason.SCOPE_MISMATCH, other_symbol.reasons)
        mainnet = coord.validator.validate_bootstrap(
            coord.bootstrap_authority, now_ms=NOW + 1_000, requested_environment=Environment.MAINNET,
            symbol="BTCUSDT", write_attempts_used=0, latency_status=PrivateLatencyStatus.UNOBSERVED,
            recovery_generation=RecoveryGeneration(0, 1), market_generation=7,
            kill_switch_mode=KillSwitchMode.NORMAL,
        )
        self.assertIn(AuthorityInvalidReason.ENVIRONMENT_NOT_TESTNET, mainnet.reasons)
        expired = coord.validator.validate_bootstrap(
            coord.bootstrap_authority, now_ms=NOW + TTL + 1, requested_environment=Environment.TESTNET,
            symbol="BTCUSDT", write_attempts_used=0, latency_status=PrivateLatencyStatus.UNOBSERVED,
            recovery_generation=RecoveryGeneration(0, 1), market_generation=7,
            kill_switch_mode=KillSwitchMode.NORMAL,
        )
        self.assertIn(AuthorityInvalidReason.AUTHORITY_EXPIRED, expired.reasons)


class BootstrapUpgradeTest(unittest.TestCase):
    def activated(self) -> BootstrapAuthorityCoordinator:
        coord = coordinator()
        coord.activate(result=unobserved_result(), eligibility=eligibility(), provenance=provenance(),
                       now_ms=NOW, environment=Environment.TESTNET)
        coord.record_write_attempt()
        return coord

    def test_6_healthy_latency_upgrades_to_normal_authority(self) -> None:
        coord = self.activated()
        # LIVE_READY 必须带 scope（真实 gate 一定给出 TESTNET_LIVE_READY）
        healthy = LiveReadinessResult(
            status=LiveReadinessStatus.LIVE_READY,
            scope=LiveReadinessScope.TESTNET_LIVE_READY,
            reasons=(),
            latency_status=PrivateLatencyStatus.HEALTHY,
        )
        activation = coord.on_private_latency_event(result=healthy, provenance=provenance(), now_ms=NOW + 5_000,
                                                    environment=Environment.TESTNET)
        self.assertIs(activation.phase, BootstrapPhase.NORMAL_READY)
        self.assertIsInstance(activation.authority, ExecutionReadinessAuthority)
        self.assertIsNone(coord.bootstrap_authority)

    def test_7_unhealthy_or_unknown_is_blocked(self) -> None:
        for latency in (PrivateLatencyStatus.UNHEALTHY, PrivateLatencyStatus.UNKNOWN):
            with self.subTest(latency=latency.value):
                coord = self.activated()
                activation = coord.on_private_latency_event(
                    result=live_ready_result(latency), provenance=provenance(), now_ms=NOW + 5_000,
                    environment=Environment.TESTNET)
                self.assertIs(activation.phase, BootstrapPhase.BLOCKED)
                self.assertIsNone(activation.authority)
                self.assertIsNone(coord.bootstrap_authority)

    def test_still_unobserved_cannot_upgrade_and_cannot_rebootstrap(self) -> None:
        coord = self.activated()
        activation = coord.on_private_latency_event(result=unobserved_result(), provenance=provenance(),
                                                    now_ms=NOW + 5_000, environment=Environment.TESTNET)
        self.assertIs(activation.phase, BootstrapPhase.BLOCKED)
        self.assertIn(LiveReadinessReason.PRIVATE_LATENCY_UNOBSERVED.value, activation.reasons)
        self.assertIsNone(coord.authority)


if __name__ == "__main__":
    unittest.main()
