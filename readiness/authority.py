"""`ExecutionReadinessAuthority`：短时执行授权 capability（P0001.9.5 §7 – §11）。

它不是"永久绿灯"，也不是密码学凭证（Python 进程内对象无法提供密码学不可伪造性，§不包含）。
它的作用是：

1. **只有 `LIVE_READY` 才能签发**（BLOCKED 绝不能生成授权，SC-5 / SC-6）；
2. 绑定**生成时的版本事实**（recovery generation / market generation / HWM activation+generation /
   risk policy 指纹 / evidence digest），供将来 Execution 边界校验；
3. **显式 TTL**（§8）：过期即拒绝（`AUTHORITY_EXPIRED`），需要重新收集 evidence + 重新 evaluate；
4. **scope 强隔离**（§14）：TESTNET 授权永远不能用于 MAINNET。

授权**不包含任何下单/撤单能力**（SC-18），也不绕过逐订单 `RiskGate`（§13 / SC-19）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum

from market.events.types import Milliseconds
from risk.types import KillSwitchMode

from readiness.types import (
    Environment,
    LiveReadinessResult,
    LiveReadinessScope,
    LiveReadinessStatus,
    ReadinessError,
    RecoveryGeneration,
)


class AuthorityError(ReadinessError):
    """授权签发/校验的契约错误。"""


class AuthorityInvalidReason(Enum):
    """授权失效原因码（§10 至少检查项）。"""

    NOT_LIVE_READY = "NOT_LIVE_READY"
    AUTHORITY_EXPIRED = "AUTHORITY_EXPIRED"
    SCOPE_MISMATCH = "SCOPE_MISMATCH"
    RECOVERY_GENERATION_CHANGED = "RECOVERY_GENERATION_CHANGED"
    MARKET_GENERATION_CHANGED = "MARKET_GENERATION_CHANGED"
    HIGH_WATERMARK_CHANGED = "HIGH_WATERMARK_CHANGED"
    KILL_SWITCH_NOT_NORMAL = "KILL_SWITCH_NOT_NORMAL"


@dataclass(frozen=True, slots=True)
class ReadinessProvenance:
    """生成授权时的**版本事实**（§9：让日志能回答"基于哪组事实"）。"""

    recovery_generation: RecoveryGeneration
    market_generation: int
    market_evidence_ts: Milliseconds
    account_snapshot_ts: Milliseconds | None
    clock_calibration_ts: Milliseconds | None
    hwm_activation_id: str | None
    hwm_generation: int
    risk_policy_fingerprint: str | None
    evidence_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.recovery_generation, RecoveryGeneration):
            raise AuthorityError("ReadinessProvenance.recovery_generation must be a RecoveryGeneration")
        for name in ("market_generation", "hwm_generation"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise AuthorityError(f"ReadinessProvenance.{name} must be a non-negative int")
        if isinstance(self.market_evidence_ts, bool) or not isinstance(self.market_evidence_ts, int):
            raise AuthorityError("ReadinessProvenance.market_evidence_ts must be an int")
        for name in ("account_snapshot_ts", "clock_calibration_ts"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise AuthorityError(f"ReadinessProvenance.{name} must be a non-negative int or None")
        for name in ("hwm_activation_id", "risk_policy_fingerprint", "evidence_digest"):
            value = getattr(self, name)
            if name == "evidence_digest":
                if not isinstance(value, str) or not value:
                    raise AuthorityError("ReadinessProvenance.evidence_digest must be a non-empty string")
                continue
            if value is not None and (not isinstance(value, str) or not value):
                raise AuthorityError(f"ReadinessProvenance.{name} must be a non-empty string or None")


@dataclass(frozen=True, slots=True)
class ExecutionReadinessAuthority:
    """短时执行授权（§7）。字段即"这张授权基于哪组事实生成"。"""

    authority_id: str
    issued_at_ms: Milliseconds
    expires_at_ms: Milliseconds
    scope: LiveReadinessScope
    environment: Environment
    recovery_generation: RecoveryGeneration
    market_generation: int
    market_evidence_ts: Milliseconds
    account_snapshot_ts: Milliseconds | None
    clock_calibration_ts: Milliseconds | None
    hwm_activation_id: str | None
    hwm_generation: int
    risk_policy_fingerprint: str | None
    evidence_digest: str
    readiness_status: LiveReadinessStatus

    def __post_init__(self) -> None:
        for name in ("authority_id", "evidence_digest"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise AuthorityError(f"ExecutionReadinessAuthority.{name} must be a non-empty string")
        for name in ("issued_at_ms", "expires_at_ms", "market_evidence_ts", "hwm_generation", "market_generation"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise AuthorityError(f"ExecutionReadinessAuthority.{name} must be a non-negative int")
        if self.expires_at_ms < self.issued_at_ms:
            raise AuthorityError("authority expires_at_ms must be >= issued_at_ms")
        if not isinstance(self.scope, LiveReadinessScope):
            raise AuthorityError("authority.scope must be a LiveReadinessScope")
        if not isinstance(self.environment, Environment):
            raise AuthorityError("authority.environment must be an Environment")
        if not isinstance(self.recovery_generation, RecoveryGeneration):
            raise AuthorityError("authority.recovery_generation must be a RecoveryGeneration")
        if self.readiness_status is not LiveReadinessStatus.LIVE_READY:
            raise AuthorityError("authority may only be issued for a LIVE_READY result (SC-5 / SC-6)")

    def __repr__(self) -> str:  # 避免把长字段列表刷进日志
        return (
            f"ExecutionReadinessAuthority(authority_id={self.authority_id!r}, scope={self.scope.value}, "
            f"issued_at_ms={self.issued_at_ms}, expires_at_ms={self.expires_at_ms})"
        )

    __str__ = __repr__


def evidence_digest(payload: dict[str, object]) -> str:
    """canonical digest（§9）：同组事实稳定、事实改变即改变。"""
    if not isinstance(payload, dict):
        raise AuthorityError("evidence_digest requires a dict payload")
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def risk_policy_fingerprint(limits: object) -> str:
    """风险策略指纹（人类配置的限额集合）。"""
    if limits is None:
        raise AuthorityError("risk_policy_fingerprint requires limits")
    payload = {
        name: getattr(limits, name, None)
        for name in (
            "max_position_qty",
            "max_position_notional",
            "max_open_order_exposure",
            "max_daily_loss",
            "max_drawdown_pct",
            "max_leverage",
            "min_liquidation_distance_bps",
            "max_mark_age_ms",
        )
    }
    payload["kill_switch_mode"] = getattr(getattr(limits, "effective_kill_switch_mode", None), "value", None)
    return evidence_digest(payload)


def issue_authority(
    result: LiveReadinessResult,
    *,
    provenance: ReadinessProvenance,
    authority_id: str,
    now_ms: Milliseconds,
    authority_ttl_ms: Milliseconds,
    environment: Environment,
) -> ExecutionReadinessAuthority:
    """由 `LIVE_READY` 结果签发授权（§5 / §8）。

    - `result.status != LIVE_READY` ⇒ 抛错（绝不生成授权）；
    - `authority_ttl_ms` 必须显式给出（不设默认业务值）。
    """
    if not isinstance(result, LiveReadinessResult):
        raise AuthorityError("issue_authority requires a LiveReadinessResult")
    if result.status is not LiveReadinessStatus.LIVE_READY:
        raise AuthorityError(f"cannot issue execution authority for {result.status.value} readiness (SC-6)")
    if result.scope is None:
        raise AuthorityError("LIVE_READY result must carry a scope")
    if not isinstance(provenance, ReadinessProvenance):
        raise AuthorityError("issue_authority requires ReadinessProvenance")
    if not isinstance(environment, Environment):
        raise AuthorityError("issue_authority requires an Environment")
    if isinstance(authority_ttl_ms, bool) or not isinstance(authority_ttl_ms, int) or authority_ttl_ms <= 0:
        raise AuthorityError("authority_ttl_ms must be a positive int (explicitly configured)")
    expected_scope = (
        LiveReadinessScope.MAINNET_LIVE_READY
        if environment is Environment.MAINNET
        else LiveReadinessScope.TESTNET_LIVE_READY
    )
    if result.scope is not expected_scope:
        raise AuthorityError(
            f"readiness scope {result.scope.value} does not match environment {environment.value}"
        )
    return ExecutionReadinessAuthority(
        authority_id=authority_id,
        issued_at_ms=int(now_ms),
        expires_at_ms=int(now_ms) + int(authority_ttl_ms),
        scope=result.scope,
        environment=environment,
        recovery_generation=provenance.recovery_generation,
        market_generation=provenance.market_generation,
        market_evidence_ts=provenance.market_evidence_ts,
        account_snapshot_ts=provenance.account_snapshot_ts,
        clock_calibration_ts=provenance.clock_calibration_ts,
        hwm_activation_id=provenance.hwm_activation_id,
        hwm_generation=provenance.hwm_generation,
        risk_policy_fingerprint=provenance.risk_policy_fingerprint,
        evidence_digest=provenance.evidence_digest,
        readiness_status=result.status,
    )


@dataclass(frozen=True, slots=True)
class AuthorityVerdict:
    """校验结果（**不是**裸 bool）。"""

    valid: bool
    reasons: tuple[AuthorityInvalidReason, ...] = ()
    details: tuple[str, ...] = ()


class ExecutionReadinessAuthorityValidator:
    """纯判定校验器（§10）：TTL + scope + generation/版本一致性 + kill switch。"""

    def validate(
        self,
        authority: ExecutionReadinessAuthority,
        *,
        now_ms: Milliseconds,
        requested_environment: Environment,
        recovery_generation: RecoveryGeneration,
        market_generation: int,
        hwm_activation_id: str | None,
        hwm_generation: int,
        kill_switch_mode: KillSwitchMode,
    ) -> AuthorityVerdict:
        if not isinstance(authority, ExecutionReadinessAuthority):
            raise AuthorityError("validate() requires an ExecutionReadinessAuthority")
        if not isinstance(requested_environment, Environment):
            raise AuthorityError("validate() requires an Environment")
        if not isinstance(recovery_generation, RecoveryGeneration):
            raise AuthorityError("validate() requires a RecoveryGeneration")
        if not isinstance(kill_switch_mode, KillSwitchMode):
            raise AuthorityError("validate() requires a KillSwitchMode")
        if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
            raise AuthorityError("validate() requires a non-negative int now_ms")
        for name, value in (("market_generation", market_generation), ("hwm_generation", hwm_generation)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise AuthorityError(f"validate() requires a non-negative int {name}")
        if hwm_activation_id is not None and (not isinstance(hwm_activation_id, str) or not hwm_activation_id):
            raise AuthorityError("validate() requires a non-empty hwm_activation_id or None")

        reasons: list[AuthorityInvalidReason] = []
        details: list[str] = []

        if now_ms > authority.expires_at_ms:
            reasons.append(AuthorityInvalidReason.AUTHORITY_EXPIRED)
            details.append(f"expired at {authority.expires_at_ms}, now {now_ms}")
        expected_scope = (
            LiveReadinessScope.MAINNET_LIVE_READY
            if requested_environment is Environment.MAINNET
            else LiveReadinessScope.TESTNET_LIVE_READY
        )
        if authority.environment is not requested_environment or authority.scope is not expected_scope:
            reasons.append(AuthorityInvalidReason.SCOPE_MISMATCH)
            details.append(
                f"authority {authority.environment.value}/{authority.scope.value} "
                f"cannot be used for {requested_environment.value}"
            )
        if not authority.recovery_generation.matches(recovery_generation):
            reasons.append(AuthorityInvalidReason.RECOVERY_GENERATION_CHANGED)
            details.append(
                f"recovery generation {authority.recovery_generation} -> {recovery_generation}"
            )
        if authority.market_generation != market_generation:
            reasons.append(AuthorityInvalidReason.MARKET_GENERATION_CHANGED)
            details.append(f"market generation {authority.market_generation} -> {market_generation}")
        if authority.hwm_activation_id != hwm_activation_id or authority.hwm_generation != hwm_generation:
            reasons.append(AuthorityInvalidReason.HIGH_WATERMARK_CHANGED)
            details.append(
                f"high-watermark {authority.hwm_activation_id}/{authority.hwm_generation} -> "
                f"{hwm_activation_id}/{hwm_generation}"
            )
        if kill_switch_mode is not KillSwitchMode.NORMAL:
            reasons.append(AuthorityInvalidReason.KILL_SWITCH_NOT_NORMAL)
            details.append(f"kill switch is {kill_switch_mode.value}")
        return AuthorityVerdict(valid=not reasons, reasons=tuple(reasons), details=tuple(details))


__all__ = [
    "AuthorityError",
    "AuthorityInvalidReason",
    "AuthorityVerdict",
    "ExecutionReadinessAuthority",
    "ExecutionReadinessAuthorityValidator",
    "ReadinessProvenance",
    "evidence_digest",
    "issue_authority",
    "risk_policy_fingerprint",
]
