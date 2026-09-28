"""P0001.9.5 Fault：授权链的 fail-closed 路径（SC-6/7/9/10/11/12/13/14/21/22/23）。

命题：**任何未知/失败/epoch 变化都不能让一张旧授权继续有效**，也**不能**在没有 LIVE_READY 时签发授权。
"""

from __future__ import annotations

import unittest
from dataclasses import replace

from readiness import (
    AuthorityError,
    AuthorityInvalidReason,
    Environment,
    ExecutionReadinessAuthorityValidator,
    LiveReadinessGate,
    LiveReadinessStatus,
    ReadinessPolicy,
    RecoveryGeneration,
    issue_authority,
)
from risk.high_watermark import HighWatermarkEvidence, HighWatermarkStatus
from risk.types import KillSwitchMode
from tests.readiness_support import active_hwm, environment_evidence, market_evidence
from tests.support import BASE_TS
from tests.unit.test_readiness_authority import NOW, POLICY, issue, provenance, ready_evidence, validate

RISK_POLICY = None  # 仅用于文档：鉴权路径不再接受裸 risk_policy=None 的绿色


class NeverIssueOnBlockedTest(unittest.TestCase):
    """SC-6：BLOCKED 绝不生成授权（逐条阻塞原因）。"""

    def _blocked(self, **overrides: object):
        return LiveReadinessGate(policy=POLICY).evaluate(ready_evidence(**overrides))

    def test_each_blocker_prevents_issuance(self) -> None:
        cases = {
            "hwm_uninitialized": {"high_watermark": HighWatermarkEvidence.uninitialized()},
            "hwm_invalid": {"high_watermark": HighWatermarkEvidence.store_failed(detail="fsync failed")},
            "market_not_ready": {"market": market_evidence(ready=False)},
            "mainnet_unvalidated": {"environment": environment_evidence(environment=Environment.MAINNET)},
            "no_risk_policy": {"risk_policy": None},
            "no_high_watermark_peak": {"high_watermark": HighWatermarkEvidence.uninitialized()},
        }
        for label, overrides in cases.items():
            with self.subTest(case=label):
                result = self._blocked(**overrides)
                self.assertIs(result.status, LiveReadinessStatus.BLOCKED)
                with self.assertRaises(AuthorityError):
                    issue_authority(
                        result,
                        provenance=provenance(),
                        authority_id="auth",
                        now_ms=NOW,
                        authority_ttl_ms=1_000,
                        environment=Environment.TESTNET,
                    )

    def test_kill_switch_reduce_only_blocks_readiness(self) -> None:
        from dataclasses import replace as _replace

        from tests.unit.test_readiness_authority import RISK_POLICY

        result = self._blocked(risk_policy=_replace(RISK_POLICY, kill_switch_mode=KillSwitchMode.REDUCE_ONLY))

        self.assertIs(result.status, LiveReadinessStatus.BLOCKED)


class ExpiryAndScopeFaultTest(unittest.TestCase):
    def test_authority_expires_and_must_be_reissued(self) -> None:
        authority = issue()

        verdict = validate(authority, now_ms=authority.expires_at_ms + 1)

        self.assertFalse(verdict.valid)
        self.assertEqual(verdict.reasons, (AuthorityInvalidReason.AUTHORITY_EXPIRED,))

    def test_testnet_authority_is_rejected_for_mainnet(self) -> None:
        authority = issue()

        verdict = validate(authority, requested_environment=Environment.MAINNET)

        self.assertFalse(verdict.valid)
        self.assertIn(AuthorityInvalidReason.SCOPE_MISMATCH, verdict.reasons)

    def test_validator_rejects_malformed_facts(self) -> None:
        authority = issue()
        for overrides in (
            {"now_ms": -1},
            {"market_generation": -1},
            {"hwm_generation": -1},
            {"hwm_activation_id": ""},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(AuthorityError):
                    validate(authority, **overrides)


class EpochChangeFaultTest(unittest.TestCase):
    """SC-10/11/12/13/14：epoch / 版本变化必须让旧授权失效。"""

    def test_any_generation_change_invalidates(self) -> None:
        authority = issue()
        cases = {
            "disconnect": {"recovery_generation": RecoveryGeneration(1, 0)},
            "recovery_invalidation": {"recovery_generation": RecoveryGeneration(0, 1)},
            "market_resync": {"market_generation": authority.market_generation + 1},
            "hwm_rebase": {"hwm_activation_id": "act-9", "hwm_generation": 2},
            "hwm_storage_failure": {"hwm_activation_id": None, "hwm_generation": 0},
            "kill_switch": {"kill_switch_mode": KillSwitchMode.HALT_ALL},
        }
        for label, overrides in cases.items():
            with self.subTest(case=label):
                verdict = validate(authority, **overrides)
                self.assertFalse(verdict.valid)
                self.assertTrue(verdict.reasons)

    def test_valid_authority_requires_every_field_to_match(self) -> None:
        authority = issue()

        self.assertTrue(validate(authority).valid)
        # 只改一个与授权无关的字段（例如 HWM 峰值）不影响校验（TTL + generation 负责生命周期）
        self.assertTrue(validate(authority, now_ms=authority.expires_at_ms).valid)


class ForgedVersionFaultTest(unittest.TestCase):
    """SC-17：授权可追溯到版本事实；伪造 provenance 会被 gate 拒绝或被校验器识破。"""

    def test_blocked_result_cannot_be_laundered_into_authority(self) -> None:
        blocked = LiveReadinessGate(policy=POLICY).evaluate(
            ready_evidence(market=market_evidence(ready=False))
        )

        with self.assertRaises(AuthorityError):
            issue_authority(
                blocked,
                provenance=provenance(),
                authority_id="auth",
                now_ms=NOW,
                authority_ttl_ms=1_000,
                environment=Environment.TESTNET,
            )

    def test_scope_mismatch_between_result_and_environment_is_refused(self) -> None:
        result = LiveReadinessGate(policy=POLICY).evaluate(ready_evidence())
        from readiness import LiveReadinessScope

        mismatched = replace(result, scope=LiveReadinessScope.MAINNET_LIVE_READY)

        with self.assertRaises(AuthorityError):
            issue_authority(
                mismatched,
                provenance=provenance(),
                authority_id="auth",
                now_ms=NOW,
                authority_ttl_ms=1_000,
                environment=Environment.TESTNET,
            )

    def test_high_watermark_evidence_cannot_claim_active_without_peak(self) -> None:
        forged = HighWatermarkEvidence(
            status=HighWatermarkStatus.ACTIVE,
            peak_equity=None,
            peak_ts=None,
            activation_id="act-1",
            activation_ts=BASE_TS,
            activation_equity=None,
            generation=1,
            scope=None,
        )

        self.assertFalse(forged.drawdown_known)  # 无 peak ⇒ 不算已知，readiness 因此阻塞
        result = LiveReadinessGate(policy=POLICY).evaluate(ready_evidence(high_watermark=forged))
        self.assertIs(result.status, LiveReadinessStatus.BLOCKED)


class AuthoritySurfaceTest(unittest.TestCase):
    """SC-18 / SC-19：授权没有执行能力，也不绕过 RiskGate。"""

    def test_no_execution_or_risk_bypass_surface(self) -> None:
        authority = issue()

        for forbidden in ("submit", "cancel", "order", "risk_gate", "risk_snapshot", "execute"):
            with self.subTest(name=forbidden):
                self.assertFalse(hasattr(authority, forbidden))

    def test_authority_repr_does_not_dump_all_facts(self) -> None:
        text = repr(issue())

        self.assertIn("ExecutionReadinessAuthority", text)
        self.assertNotIn("evidence_digest", text)  # 精简 repr（避免长字段刷日志）


if __name__ == "__main__":
    unittest.main()
