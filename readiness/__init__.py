"""Live Readiness 领域（P0001.9.4）：回答「当前 runtime / account / environment 是否具备进入真实下单阶段的前置条件？」

与 Risk 的责任边界（提案 §8）：

- `LiveReadinessGate` = **session / deployment 前置条件**（一次评估，回答 LIVE_READY / BLOCKED）；
- `RiskGate` = **每一笔** OrderProposal 的即时风险判断。

两者互不替代：`LIVE_READY` 不得绕过 `RiskGate`；`RiskGate ALLOW` 也不代表 `LIVE_READY`。
本包**不**包含任何下单 / 撤单能力。
"""

from __future__ import annotations

from readiness.authority import (
    AuthorityError,
    AuthorityInvalidReason,
    AuthorityVerdict,
    ExecutionReadinessAuthority,
    ExecutionReadinessAuthorityValidator,
    ReadinessProvenance,
    evidence_digest,
    issue_authority,
    risk_policy_fingerprint,
)
from readiness.collector import (
    CollectedReadinessEvidence,
    CollectionError,
    ReadinessEvidenceCollector,
    current_kill_switch_mode,
    recovery_generation,
)
from readiness.evidence import (
    account_evidence,
    environment_evidence,
    exchange_available_balance,
    historical_risk_baseline_from_income,
    historical_risk_evidence,
    historical_risk_evidence_from_snapshot,
    private_stream_evidence,
)
from readiness.gate import LiveReadinessGate
from readiness.types import (
    AccountEvidence,
    Environment,
    EnvironmentEvidence,
    EnvironmentValidationEvidence,
    EnvironmentValidationStatus,
    HistoricalRiskEvidence,
    LiveReadinessEvidence,
    LiveReadinessReason,
    LiveReadinessResult,
    LiveReadinessScope,
    LiveReadinessStatus,
    LiveRiskPolicy,
    PrivateLatencyStatus,
    PrivateStreamEvidence,
    ReadinessError,
    ReadinessPolicy,
    RecoveryGeneration,
)

__all__ = [
    "AccountEvidence",
    "AuthorityError",
    "AuthorityInvalidReason",
    "AuthorityVerdict",
    "CollectedReadinessEvidence",
    "CollectionError",
    "EnvironmentValidationEvidence",
    "EnvironmentValidationStatus",
    "ExecutionReadinessAuthority",
    "ExecutionReadinessAuthorityValidator",
    "ReadinessEvidenceCollector",
    "ReadinessProvenance",
    "RecoveryGeneration",
    "current_kill_switch_mode",
    "evidence_digest",
    "issue_authority",
    "recovery_generation",
    "risk_policy_fingerprint",
    "Environment",
    "EnvironmentEvidence",
    "HistoricalRiskEvidence",
    "LiveReadinessEvidence",
    "LiveReadinessGate",
    "LiveReadinessReason",
    "LiveReadinessResult",
    "LiveReadinessScope",
    "LiveReadinessStatus",
    "LiveRiskPolicy",
    "PrivateLatencyStatus",
    "PrivateStreamEvidence",
    "ReadinessError",
    "ReadinessPolicy",
    "account_evidence",
    "environment_evidence",
    "exchange_available_balance",
    "historical_risk_baseline_from_income",
    "historical_risk_evidence",
    "historical_risk_evidence_from_snapshot",
    "private_stream_evidence",
]

from readiness.bootstrap import (
    AuthorityKind,
    BOOTSTRAP_MAX_NOTIONAL_USDT,
    BOOTSTRAP_MAX_ORDERS,
    BootstrapActivation,
    BootstrapAuthority,
    BootstrapAuthorityCoordinator,
    BootstrapEligibility,
    BootstrapPhase,
    BootstrapWriteGate,
    issue_bootstrap_authority,
)

__all__ += [
    "AuthorityKind",
    "BOOTSTRAP_MAX_NOTIONAL_USDT",
    "BOOTSTRAP_MAX_ORDERS",
    "BootstrapActivation",
    "BootstrapAuthority",
    "BootstrapAuthorityCoordinator",
    "BootstrapEligibility",
    "BootstrapPhase",
    "BootstrapWriteGate",
    "issue_bootstrap_authority",
]
