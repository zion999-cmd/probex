"""P0001.9.7.1 §三：orchestrator 识别 BOOTSTRAP lifecycle（新增暴露门）。"""

from __future__ import annotations

import unittest

from readiness.authority import AuthorityInvalidReason
from readiness.bootstrap import (
    BootstrapAuthorityCoordinator,
    BootstrapEligibility,
    BootstrapWriteGate,
)
from readiness.types import (
    Environment,
    LiveReadinessReason,
    LiveReadinessResult,
    LiveReadinessStatus,
    PrivateLatencyStatus,
    RecoveryGeneration,
)
from tests.orchestration_support import NOW, OrchestrationStack, context


def _bootstrap() -> tuple[BootstrapAuthorityCoordinator, object]:
    coord = BootstrapAuthorityCoordinator(symbol="BTCUSDT", bootstrap_ttl_ms=600_000, normal_ttl_ms=600_000)
    result = LiveReadinessResult(
        status=LiveReadinessStatus.BOOTSTRAP_ELIGIBLE,
        scope=None,
        reasons=(LiveReadinessReason.PRIVATE_LATENCY_UNOBSERVED,),
        latency_status=PrivateLatencyStatus.UNOBSERVED,
    )
    provenance = __import__("readiness.authority", fromlist=["ReadinessProvenance"]).ReadinessProvenance(
        recovery_generation=RecoveryGeneration(0, 0),
        market_generation=1,
        market_evidence_ts=NOW,
        account_snapshot_ts=NOW,
        clock_calibration_ts=NOW,
        hwm_activation_id="act-1",
        hwm_generation=1,
        risk_policy_fingerprint="risk:x",
        evidence_digest="sha256:x",
    )
    eligibility = BootstrapEligibility(
        recovery_status=__import__(
            "connectors.binance.private.recovery", fromlist=["RecoveryStatus"]
        ).RecoveryStatus.RECOVERED,
        market_ready=True,
        private_continuity_valid=True,
        account_flat=True,
        probex_open_orders=0,
        foreign_open_orders=0,
        uncertain_exposure_qty=0.0,
        latency_status=PrivateLatencyStatus.UNOBSERVED,
        environment=Environment.TESTNET,
    )
    activation = coord.activate(result=result, eligibility=eligibility, provenance=provenance, now_ms=NOW,
                                environment=Environment.TESTNET)
    assert activation.authority is not None
    return coord, activation.authority


class OrchestratorBootstrapTest(unittest.TestCase):
    def stack(self, *, attempts_used: int) -> OrchestrationStack:
        coord, bootstrap_authority = _bootstrap()
        for _ in range(attempts_used):
            coord.record_write_attempt()
        return OrchestrationStack.build(
            authority_context=context(
                authority=bootstrap_authority,
                bootstrap_gate=BootstrapWriteGate(coord),
                latency_status=PrivateLatencyStatus.UNOBSERVED,
                private_continuity_valid=True,
            )
        )

    def test_fresh_bootstrap_allows_new_exposure(self) -> None:
        stack = self.stack(attempts_used=0)
        notes: list[str] = []
        self.assertTrue(stack.orchestrator._can_increase_exposure(notes=notes))
        self.assertEqual(notes, [])

    def test_exhausted_bootstrap_blocks_new_exposure_but_not_cancel(self) -> None:
        """额度耗尽 ⇒ 新增暴露（PLACE / REPLACE）被拒；降低风险的 CANCEL 不经过此门。"""
        stack = self.stack(attempts_used=1)
        notes: list[str] = []
        self.assertFalse(stack.orchestrator._can_increase_exposure(notes=notes))
        self.assertIn(
            f"authority_invalid:{AuthorityInvalidReason.ORDERS_USED_EXCEEDS_MAX.value}", notes
        )

    def test_superseded_bootstrap_blocks_new_exposure(self) -> None:
        stack = self.stack(attempts_used=0)
        current = stack.orchestrator.authority_provider()
        superseded_ctx = context(
            authority=current.authority,
            bootstrap_gate=current.bootstrap_gate,
            latency_status=PrivateLatencyStatus.HEALTHY,
            private_continuity_valid=True,
        )
        stack.orchestrator.authority_provider = lambda: superseded_ctx  # type: ignore[assignment]
        notes: list[str] = []
        self.assertFalse(stack.orchestrator._can_increase_exposure(notes=notes))
        self.assertIn(
            f"authority_invalid:{AuthorityInvalidReason.BOOTSTRAP_SUPERSEDED.value}", notes
        )

    def test_bootstrap_without_gate_is_treated_as_not_usable(self) -> None:
        coord, bootstrap_authority = _bootstrap()
        del coord
        stack = OrchestrationStack.build(
            authority_context=context(authority=bootstrap_authority, bootstrap_gate=None,
                                      latency_status=PrivateLatencyStatus.UNOBSERVED,
                                      private_continuity_valid=True)
        )
        notes: list[str] = []
        self.assertFalse(stack.orchestrator._can_increase_exposure(notes=notes))
        self.assertTrue(any("authority_invalid" in note for note in notes))

    def test_continuity_lost_after_collect_blocks_new_exposure(self) -> None:
        """JIT：collect 之后 continuity 失效（WS reconnect）⇒ 旧 authority 不得再用于新增暴露。"""
        coord, bootstrap_authority = _bootstrap()
        stack = OrchestrationStack.build(
            authority_context=context(authority=bootstrap_authority, bootstrap_gate=BootstrapWriteGate(coord),
                                      latency_status=PrivateLatencyStatus.UNOBSERVED,
                                      private_continuity_valid=False)
        )
        notes: list[str] = []
        self.assertFalse(stack.orchestrator._can_increase_exposure(notes=notes))
        self.assertIn(
            f"authority_invalid:{AuthorityInvalidReason.PRIVATE_CONTINUITY_INVALID.value}", notes
        )


if __name__ == "__main__":
    unittest.main()
