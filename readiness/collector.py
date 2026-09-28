"""`ReadinessEvidenceCollector`：从真实 Owner 读取事实并组装 readiness 证据（P0001.9.5 §2）。

调用方不再逐字段拼装生产级 `LiveReadinessEvidence`：

| 输入 | Owner |
| --- | --- |
| recovery 状态 + generation | `StartupRecovery`（`state` / `reason_log`） |
| private stream 事实 + generation | `PrivateAccountRuntime`（`telemetry` / `snapshot_boundary` / `discontinuity_events`） |
| account 事实 | `AccountSnapshotObservation` |
| historical risk 已知性 | 当前 `RiskSnapshot` |
| high-watermark 证据 | `HighWatermarkTracker.evidence()`（**只能**从这里来，§5） |
| market 事实 | `MarketReadinessEvidence`（产品侧，§3） |
| environment 验收 | `EnvironmentValidationEvidence`（typed，§4） |
| risk policy | 人类显式配置（`LiveRiskPolicy`） |

本模块只做**读取与映射**：不触网、不评估、不签发授权。
"""

from __future__ import annotations

from dataclasses import dataclass

from market.events.types import Milliseconds
from market.readiness import MarketReadinessEvidence
from risk.high_watermark import HighWatermarkEvidence, HighWatermarkTracker
from risk.snapshot import build_risk_snapshot
from risk.types import KillSwitchMode

from connectors.binance.private.account import AccountSnapshotObservation
from connectors.binance.private.recovery import RecoveryStatus, StartupRecovery
from connectors.binance.private.runtime import PrivateAccountRuntime
from readiness.authority import ReadinessProvenance, evidence_digest, risk_policy_fingerprint
from readiness.evidence import (
    account_evidence,
    historical_risk_evidence_from_snapshot,
    private_stream_evidence,
)
from readiness.types import (
    EnvironmentEvidence,
    LiveReadinessEvidence,
    LiveRiskPolicy,
    ReadinessError,
    RecoveryGeneration,
)


class CollectionError(ReadinessError):
    """收集失败（缺少必要 Owner 事实）。"""


@dataclass(frozen=True, slots=True)
class CollectedReadinessEvidence:
    """证据 + 生成它的**版本事实**（供签发授权绑定）。"""

    evidence: LiveReadinessEvidence
    provenance: ReadinessProvenance


def recovery_generation(
    *, runtime: PrivateAccountRuntime, recovery: StartupRecovery
) -> RecoveryGeneration:
    """由真实 runtime/recovery 事实计算 recovery epoch（§6）。

    - `discontinuity_count` = `runtime.discontinuity_events` 的长度（disconnect / listenKey recreate / stop）；
    - `invalidation_count` = `recovery.reason_log` 的长度（recovery 被显式失效的次数）。
    """
    if not isinstance(runtime, PrivateAccountRuntime):
        raise CollectionError("recovery_generation requires a PrivateAccountRuntime")
    if not isinstance(recovery, StartupRecovery):
        raise CollectionError("recovery_generation requires a StartupRecovery")
    return RecoveryGeneration(
        discontinuity_count=len(runtime.discontinuity_events),
        invalidation_count=len(recovery.reason_log),
    )


class ReadinessEvidenceCollector:
    """受控 evidence 收集器（§2）。"""

    def __init__(
        self,
        *,
        runtime: PrivateAccountRuntime,
        recovery: StartupRecovery,
        market: MarketReadinessEvidence,
        environment: EnvironmentEvidence,
        risk_policy: LiveRiskPolicy | None,
        high_watermark: HighWatermarkEvidence | HighWatermarkTracker,
    ) -> None:
        for name, value, expected in (
            ("runtime", runtime, PrivateAccountRuntime),
            ("recovery", recovery, StartupRecovery),
            ("market", market, MarketReadinessEvidence),
            ("environment", environment, EnvironmentEvidence),
        ):
            if not isinstance(value, expected):
                raise CollectionError(f"ReadinessEvidenceCollector.{name} must be a {expected.__name__}")
        if isinstance(high_watermark, HighWatermarkTracker):
            # 正式路径：每次 collect 都向 tracker 取**当前**证据（§5）
            self._hwm_provider = high_watermark.evidence
        elif isinstance(high_watermark, HighWatermarkEvidence):
            # 显式一次性证据（例如根本没有 tracker 的 harness）；文档化、非默认
            self._hwm_provider = lambda: high_watermark
        else:
            raise CollectionError(
                "ReadinessEvidenceCollector.high_watermark must be a HighWatermarkTracker or HighWatermarkEvidence"
            )
        if risk_policy is not None and not isinstance(risk_policy, LiveRiskPolicy):
            raise CollectionError("ReadinessEvidenceCollector.risk_policy must be a LiveRiskPolicy or None")
        self._runtime = runtime
        self._recovery = recovery
        self._market = market
        self._environment = environment
        self._risk_policy = risk_policy

    @classmethod
    def from_high_watermark_tracker(cls, tracker: HighWatermarkTracker, **kwargs: object) -> "ReadinessEvidenceCollector":
        """正式路径：HWM 证据**只能**来自 tracker（§5）。"""
        if not isinstance(tracker, HighWatermarkTracker):
            raise CollectionError("from_high_watermark_tracker requires a HighWatermarkTracker")
        return cls(high_watermark=tracker, **kwargs)  # type: ignore[arg-type]

    def collect(
        self,
        *,
        accounting: object,
        now_ms: Milliseconds,
        day_start_ts: Milliseconds | None = None,
        historical_baseline: object | None = None,
        exchange_available_balance: object | None = None,
    ) -> CollectedReadinessEvidence:
        """读取全部真实事实并组装（含 version provenance 与 digest）。"""
        high_watermark = self._hwm_provider()
        observation = self._runtime.latest_snapshot
        if not isinstance(observation, AccountSnapshotObservation):
            raise CollectionError("no account snapshot available; call runtime.refresh_snapshot() first")
        snapshot = build_risk_snapshot(
            accounting,  # type: ignore[arg-type]
            symbol=self._runtime.config.symbol,
            now_ms=now_ms,
            day_start_ts=day_start_ts,
            exchange_available_balance=exchange_available_balance,  # type: ignore[arg-type]
            historical_baseline=historical_baseline,  # type: ignore[arg-type]
            high_watermark=high_watermark,
        )
        hwm = high_watermark
        evidence = LiveReadinessEvidence(
            now_ms=now_ms,
            recovery_status=self._recovery.state,
            private_stream=private_stream_evidence(
                self._runtime.telemetry, boundary_present=self._runtime.snapshot_boundary is not None
            ),
            account=account_evidence(observation),
            historical_risk=historical_risk_evidence_from_snapshot(snapshot),
            environment=self._environment,
            market=self._market,
            risk_policy=self._risk_policy,
            high_watermark=hwm,
        )
        generation = recovery_generation(runtime=self._runtime, recovery=self._recovery)
        fingerprint = (
            None if self._risk_policy is None else risk_policy_fingerprint(self._risk_policy.to_limits())
        )
        digest = evidence_digest(
            {
                "environment": self._environment.environment.value,
                "validation_status": self._environment.validation.validation_status.value,
                "recovery_generation": [generation.discontinuity_count, generation.invalidation_count],
                "recovery_status": evidence.recovery_status.value,
                "private_listen_key": evidence.private_stream.listen_key_state,
                "private_continuity": evidence.private_stream.continuity_assumed,
                "private_boundary": evidence.private_stream.boundary_present,
                "clock_offset_ms": None
                if evidence.private_stream.clock_calibration is None
                else evidence.private_stream.clock_calibration.offset_ms,
                "clock_measured_at_ms": None
                if evidence.private_stream.clock_calibration is None
                else evidence.private_stream.clock_calibration.measured_at_ms,
                "account_captured_at": evidence.account.available_balance_captured_at,
                "account_available_balance": evidence.account.available_balance,
                "market_generation": self._market.generation,
                "market_ready": self._market.ready,
                "market_observed_at": self._market.observed_at,
                "historical_daily_pnl_known": evidence.historical_risk.daily_pnl_known,
                "historical_drawdown_known": evidence.historical_risk.drawdown_known,
                "hwm_status": hwm.status.value,
                "hwm_activation_id": hwm.activation_id,
                "hwm_generation": hwm.generation,
                "hwm_peak_equity": hwm.peak_equity,
                "risk_policy_fingerprint": fingerprint,
            }
        )
        provenance = ReadinessProvenance(
            recovery_generation=generation,
            market_generation=self._market.generation,
            market_evidence_ts=self._market.observed_at,
            account_snapshot_ts=evidence.account.available_balance_captured_at,
            clock_calibration_ts=None
            if evidence.private_stream.clock_calibration is None
            else evidence.private_stream.clock_calibration.measured_at_ms,
            hwm_activation_id=hwm.activation_id,
            hwm_generation=hwm.generation,
            risk_policy_fingerprint=fingerprint,
            evidence_digest=digest,
        )
        return CollectedReadinessEvidence(evidence=evidence, provenance=provenance)


def current_kill_switch_mode(*, policy: LiveRiskPolicy | None) -> KillSwitchMode:
    """当前 kill switch（供校验器使用；无策略 ⇒ 视为不可用）。"""
    if policy is None:
        return KillSwitchMode.HALT_ALL
    return policy.kill_switch_mode


__all__ = [
    "CollectedReadinessEvidence",
    "CollectionError",
    "ReadinessEvidenceCollector",
    "current_kill_switch_mode",
    "recovery_generation",
]
