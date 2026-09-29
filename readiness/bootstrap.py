"""Cold-start BOOTSTRAP authority（P0001.9.7.1）。

它解决的问题是**首次激活的死锁**：private latency 样本只能来自真实 private 业务事件，
而真实业务事件又需要授权——于是在"干净、安静、flat"的账户上永远无法取得第一个样本。

本模块**不**放宽 `PRIVATE_LATENCY_UNKNOWN`，也**不**引入任何"探测单"旁路。它做的是：

1. 把"从未有可测事件"显式建模为 `PrivateLatencyStatus.UNOBSERVED`（由 `readiness.gate` 分类）；
2. 只在**全部**冷启动前置事实成立时，签发一张**比 NORMAL 更窄**的一次性 `BootstrapAuthority`
   （TESTNET + symbol、短 TTL、`max_orders=1`、post-only、notional ≤ 100 USDT）；
3. 首个**可测量**的 private 事件到达后，bootstrap **立即失效**（`BOOTSTRAP_SUPERSEDED`），
   必须重新 collect readiness：`HEALTHY` 才签发 NORMAL authority；`UNHEALTHY`/`UNKNOWN`/仍 `UNOBSERVED` ⇒ `BLOCKED`。

`max_orders` 的计数口径：**发生过一次真实 write attempt 即消耗**，不是等 `CONFIRMED_ACCEPTED`。
否则第一笔若为 `UNKNOWN`，会被误判成"还没写入"而重提第二笔——正是 uncertain-exposure 原则禁止的。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from market.events.types import Milliseconds
from risk.types import KillSwitchMode

from connectors.binance.private.recovery import RecoveryStatus

from readiness.authority import (
    AuthorityError,
    AuthorityInvalidReason,
    AuthorityVerdict,
    ExecutionReadinessAuthority,
    ExecutionReadinessAuthorityValidator,
    ReadinessProvenance,
    issue_authority,
)
from readiness.types import (
    Environment,
    LiveReadinessReason,
    LiveReadinessResult,
    LiveReadinessStatus,
    PrivateLatencyStatus,
    ReadinessError,
)

#: BOOTSTRAP 只允许一笔（人类裁决：一次性）
BOOTSTRAP_MAX_ORDERS = 1

#: BOOTSTRAP 的 notional 硬上限（人类裁决：≤ 100 USDT）
BOOTSTRAP_MAX_NOTIONAL_USDT = 100.0


class AuthorityKind(Enum):
    """授权种类（**互不替代**：NORMAL 不接受 UNOBSERVED，BOOTSTRAP 只用于冷启动）。"""

    NORMAL = "NORMAL"
    BOOTSTRAP = "BOOTSTRAP"


@dataclass(frozen=True, slots=True)
class BootstrapEligibility:
    """冷启动的**全部**附加前置事实（逐项显式，无隐式默认）。"""

    recovery_status: RecoveryStatus
    market_ready: bool
    private_continuity_valid: bool
    account_flat: bool
    probex_open_orders: int
    foreign_open_orders: int
    uncertain_exposure_qty: float
    latency_status: PrivateLatencyStatus
    environment: Environment

    def __post_init__(self) -> None:
        if not isinstance(self.recovery_status, RecoveryStatus):
            raise ReadinessError("BootstrapEligibility.recovery_status must be a RecoveryStatus")
        for name in ("market_ready", "private_continuity_valid", "account_flat"):
            if not isinstance(getattr(self, name), bool):
                raise ReadinessError(f"BootstrapEligibility.{name} must be a bool")
        for name in ("probex_open_orders", "foreign_open_orders"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ReadinessError(f"BootstrapEligibility.{name} must be a non-negative int")
        if isinstance(self.uncertain_exposure_qty, bool) or not isinstance(self.uncertain_exposure_qty, (int, float)):
            raise ReadinessError("BootstrapEligibility.uncertain_exposure_qty must be a number")
        if not isinstance(self.latency_status, PrivateLatencyStatus):
            raise ReadinessError("BootstrapEligibility.latency_status must be a PrivateLatencyStatus")
        if not isinstance(self.environment, Environment):
            raise ReadinessError("BootstrapEligibility.environment must be an Environment")

    def problems(self) -> tuple[str, ...]:
        """返回**全部**不满足项（fail closed 且便于定位）。"""
        problems: list[str] = []
        if self.recovery_status is not RecoveryStatus.RECOVERED:
            problems.append(f"recovery is {self.recovery_status.value}, not RECOVERED")
        if not self.market_ready:
            problems.append("market is not ready")
        if not self.private_continuity_valid:
            problems.append("private continuity is not valid")
        if not self.account_flat:
            problems.append("account is not flat")
        if self.probex_open_orders != 0:
            problems.append(f"probex open orders = {self.probex_open_orders}, expected 0")
        if self.foreign_open_orders != 0:
            problems.append(f"foreign open orders = {self.foreign_open_orders}, expected 0")
        if float(self.uncertain_exposure_qty) != 0.0:
            problems.append(f"uncertain exposure = {self.uncertain_exposure_qty}, expected 0")
        if self.latency_status is not PrivateLatencyStatus.UNOBSERVED:
            problems.append(f"private latency is {self.latency_status.value}, expected UNOBSERVED")
        if self.environment is not Environment.TESTNET:
            problems.append(f"environment is {self.environment.value}, bootstrap is TESTNET only")
        return tuple(problems)


@dataclass(frozen=True, slots=True)
class BootstrapAuthority:
    """一次性冷启动授权（比 NORMAL 更窄；字段全部显式，不复用 NORMAL 的隐式默认）。"""

    authority_id: str
    bootstrap_generation: int
    issued_at_ms: Milliseconds
    expires_at_ms: Milliseconds
    environment: Environment
    symbol: str
    max_orders: int
    post_only_required: bool
    max_notional_usdt: float
    recovery_generation: object
    market_generation: int
    market_evidence_ts: Milliseconds
    evidence_digest: str
    readiness_status: LiveReadinessStatus

    @property
    def kind(self) -> AuthorityKind:
        return AuthorityKind.BOOTSTRAP

    def __post_init__(self) -> None:
        for name in ("authority_id", "symbol", "evidence_digest"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise AuthorityError(f"BootstrapAuthority.{name} must be a non-empty string")
        for name in ("bootstrap_generation", "issued_at_ms", "expires_at_ms", "market_generation",
                     "market_evidence_ts"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise AuthorityError(f"BootstrapAuthority.{name} must be a non-negative int")
        if self.expires_at_ms < self.issued_at_ms:
            raise AuthorityError("BootstrapAuthority.expires_at_ms must be >= issued_at_ms")
        if self.environment is not Environment.TESTNET:
            raise AuthorityError("BootstrapAuthority is TESTNET only (Mainnet is never bootstrap-able)")
        if self.max_orders != BOOTSTRAP_MAX_ORDERS:
            raise AuthorityError(f"BootstrapAuthority.max_orders must be {BOOTSTRAP_MAX_ORDERS}")
        if self.post_only_required is not True:
            raise AuthorityError("BootstrapAuthority.post_only_required must be True")
        if isinstance(self.max_notional_usdt, bool) or not isinstance(self.max_notional_usdt, (int, float)):
            raise AuthorityError("BootstrapAuthority.max_notional_usdt must be a number")
        if not 0 < float(self.max_notional_usdt) <= BOOTSTRAP_MAX_NOTIONAL_USDT:
            raise AuthorityError(
                f"BootstrapAuthority.max_notional_usdt must be in (0, {BOOTSTRAP_MAX_NOTIONAL_USDT}]"
            )
        if self.readiness_status is not LiveReadinessStatus.BOOTSTRAP_ELIGIBLE:
            raise AuthorityError(
                "BootstrapAuthority may only be issued for a BOOTSTRAP_ELIGIBLE result "
                "(never for LIVE_READY / BLOCKED)"
            )

    def __repr__(self) -> str:
        return (
            f"BootstrapAuthority(authority_id={self.authority_id!r}, symbol={self.symbol!r}, "
            f"issued_at_ms={self.issued_at_ms}, expires_at_ms={self.expires_at_ms}, "
            f"max_orders={self.max_orders}, max_notional_usdt={self.max_notional_usdt})"
        )

    __str__ = __repr__


def issue_bootstrap_authority(
    result: LiveReadinessResult,
    *,
    eligibility: BootstrapEligibility,
    provenance: ReadinessProvenance,
    authority_id: str,
    bootstrap_generation: int,
    symbol: str,
    now_ms: Milliseconds,
    authority_ttl_ms: Milliseconds,
    environment: Environment,
    max_notional_usdt: float,
) -> BootstrapAuthority:
    """在**全部**冷启动前置事实成立时签发一次性 BOOTSTRAP authority。

    任何一项不满足 ⇒ 抛 `AuthorityError`（附完整问题清单），绝不"部分放行"。
    """
    if not isinstance(result, LiveReadinessResult):
        raise AuthorityError("issue_bootstrap_authority requires a LiveReadinessResult")
    if not isinstance(eligibility, BootstrapEligibility):
        raise AuthorityError("issue_bootstrap_authority requires BootstrapEligibility")
    if not isinstance(provenance, ReadinessProvenance):
        raise AuthorityError("issue_bootstrap_authority requires ReadinessProvenance")
    if not isinstance(environment, Environment):
        raise AuthorityError("issue_bootstrap_authority requires an Environment")
    if not isinstance(symbol, str) or not symbol:
        raise AuthorityError("issue_bootstrap_authority requires a non-empty symbol")
    if isinstance(authority_ttl_ms, bool) or not isinstance(authority_ttl_ms, int) or authority_ttl_ms <= 0:
        raise AuthorityError("authority_ttl_ms must be a positive int (explicitly configured; no default)")
    if isinstance(bootstrap_generation, bool) or not isinstance(bootstrap_generation, int) \
            or bootstrap_generation < 0:
        raise AuthorityError("bootstrap_generation must be a non-negative int")
    if not isinstance(authority_id, str) or not authority_id:
        raise AuthorityError("authority_id must be a non-empty string")
    if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
        raise AuthorityError("now_ms must be a non-negative int")

    problems: list[str] = []
    # ① readiness 必须是 BOOTSTRAP_ELIGIBLE（既不是 LIVE_READY，也不是 BLOCKED）
    if result.status is not LiveReadinessStatus.BOOTSTRAP_ELIGIBLE:
        problems.append(f"readiness status is {result.status.value}, expected BOOTSTRAP_ELIGIBLE")
    if result.latency_status is not PrivateLatencyStatus.UNOBSERVED:
        problems.append(
            f"result latency_status is "
            f"{None if result.latency_status is None else result.latency_status.value}, expected UNOBSERVED"
        )
    # ② 附加事实
    problems.extend(eligibility.problems())
    # ③ 环境 / 上限
    if environment is not Environment.TESTNET:
        problems.append(f"environment is {environment.value}, bootstrap is TESTNET only")
    if eligibility.environment is not environment:
        problems.append("eligibility.environment does not match the requested environment")
    if float(max_notional_usdt) <= 0 or float(max_notional_usdt) > BOOTSTRAP_MAX_NOTIONAL_USDT:
        problems.append(
            f"max_notional_usdt {max_notional_usdt} is outside (0, {BOOTSTRAP_MAX_NOTIONAL_USDT}]"
        )
    if problems:
        raise AuthorityError("cannot issue BOOTSTRAP authority: " + "; ".join(problems))

    return BootstrapAuthority(
        authority_id=authority_id,
        bootstrap_generation=int(bootstrap_generation),
        issued_at_ms=int(now_ms),
        expires_at_ms=int(now_ms) + int(authority_ttl_ms),
        environment=environment,
        symbol=symbol,
        max_orders=BOOTSTRAP_MAX_ORDERS,
        post_only_required=True,
        max_notional_usdt=float(max_notional_usdt),
        recovery_generation=provenance.recovery_generation,
        market_generation=provenance.market_generation,
        market_evidence_ts=provenance.market_evidence_ts,
        evidence_digest=provenance.evidence_digest,
        readiness_status=result.status,
    )


class BootstrapPhase(Enum):
    """冷启动闭环的相位。"""

    ELIGIBLE = "ELIGIBLE"
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    NORMAL_READY = "NORMAL_READY"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class BootstrapActivation:
    """一次闭环推进的结果（**不是**裸 bool）。"""

    phase: BootstrapPhase
    authority: BootstrapAuthority | ExecutionReadinessAuthority | None = None
    reasons: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return self.authority is not None


@dataclass(slots=True)
class BootstrapAuthorityCoordinator:
    """冷启动闭环 Owner（P0001.9.7.1 §三）。

    它拥有：bootstrap authority 的签发与**立即失效**、write attempt 计数（口径 = attempt），
    以及"首笔可测事件后必须升级到 NORMAL 或 BLOCKED"的判定。
    """

    symbol: str
    bootstrap_ttl_ms: Milliseconds
    normal_ttl_ms: Milliseconds
    max_notional_usdt: float = BOOTSTRAP_MAX_NOTIONAL_USDT
    authority_id_prefix: str = "bootstrap"
    validator: ExecutionReadinessAuthorityValidator = field(
        default_factory=ExecutionReadinessAuthorityValidator
    )
    _authority: BootstrapAuthority | ExecutionReadinessAuthority | None = None
    _bootstrap: BootstrapAuthority | None = None
    _generation: int = 0
    _write_attempts: int = 0
    _phase: BootstrapPhase = BootstrapPhase.ELIGIBLE

    # ------------------------------------------------------------------ 状态

    @property
    def phase(self) -> BootstrapPhase:
        return self._phase

    @property
    def authority(self) -> BootstrapAuthority | ExecutionReadinessAuthority | None:
        return self._authority

    @property
    def bootstrap_authority(self) -> BootstrapAuthority | None:
        return self._bootstrap

    @property
    def write_attempts(self) -> int:
        """**已消耗的 write attempt 数**（口径 = attempt，不是 acceptance）。"""
        return self._write_attempts

    @property
    def bootstrap_generation(self) -> int:
        """已签发的 bootstrap 代数（JIT 重新签发会递增；与新 authority 的 `bootstrap_generation` 一致）。"""
        return self._generation

    @property
    def bootstrap_orders_remaining(self) -> int:
        if self._bootstrap is None:
            return 0
        return max(0, self._bootstrap.max_orders - self._write_attempts)

    # ------------------------------------------------------------------ 动作

    def activate(
        self,
        *,
        result: LiveReadinessResult,
        eligibility: BootstrapEligibility,
        provenance: ReadinessProvenance,
        now_ms: Milliseconds,
        environment: Environment,
    ) -> BootstrapActivation:
        """冷启动激活：满足全部条件 ⇒ `ACTIVE`（携带 BOOTSTRAP authority）；否则 `BLOCKED`。"""
        if result.status is not LiveReadinessStatus.BOOTSTRAP_ELIGIBLE:
            # 已经是 LIVE_READY 的情况也走 NORMAL（不必 bootstrap）
            if result.status is LiveReadinessStatus.LIVE_READY:
                return self._issue_normal(result=result, provenance=provenance, now_ms=now_ms,
                                          environment=environment)
            return BootstrapActivation(
                phase=BootstrapPhase.BLOCKED,
                reasons=tuple(r.value for r in result.reasons) or ("readiness is not BOOTSTRAP_ELIGIBLE",),
            )
        # JIT 重新签发**不得**重置已消耗的额度：已用过唯一一次 write attempt 后，
        # 任何再次 activate 都必须被拒（否则可循环 re-issue 拿到无限次写入）。
        if self._write_attempts >= BOOTSTRAP_MAX_ORDERS:
            self._phase = BootstrapPhase.BLOCKED
            self._authority = None
            self._bootstrap = None
            return BootstrapActivation(
                phase=BootstrapPhase.BLOCKED,
                reasons=(AuthorityInvalidReason.ORDERS_USED_EXCEEDS_MAX.value,
                         f"bootstrap write attempt already consumed ({self._write_attempts}/"
                         f"{BOOTSTRAP_MAX_ORDERS}); re-issuance is refused"),
            )
        self._generation += 1
        try:
            authority = issue_bootstrap_authority(
                result,
                eligibility=eligibility,
                provenance=provenance,
                authority_id=f"{self.authority_id_prefix}-{self.symbol}-{self._generation}",
                bootstrap_generation=self._generation,
                symbol=self.symbol,
                now_ms=now_ms,
                authority_ttl_ms=self.bootstrap_ttl_ms,
                environment=environment,
                max_notional_usdt=self.max_notional_usdt,
            )
        except AuthorityError as exc:
            self._phase = BootstrapPhase.BLOCKED
            self._authority = None
            self._bootstrap = None
            return BootstrapActivation(phase=BootstrapPhase.BLOCKED, reasons=(str(exc),))
        self._authority = authority
        self._bootstrap = authority
        self._phase = BootstrapPhase.ACTIVE
        return BootstrapActivation(phase=BootstrapPhase.ACTIVE, authority=authority)

    def record_write_attempt(self) -> int:
        """记录一次**真实 write attempt**（无论结果 ACCEPTED / REJECTED / UNKNOWN 都消耗额度）。"""
        self._write_attempts += 1
        return self._write_attempts

    def validate_current(
        self,
        *,
        now_ms: Milliseconds,
        requested_environment: Environment,
        latency_status: PrivateLatencyStatus,
        recovery_generation: object,
        market_generation: int,
        kill_switch_mode: KillSwitchMode,
    ) -> AuthorityVerdict:
        """校验当前 bootstrap authority（`BOOTSTRAP_SUPERSEDED` 在此判定）。"""
        if self._bootstrap is None:
            raise AuthorityError("no active bootstrap authority to validate")
        return self.validator.validate_bootstrap(
            self._bootstrap,
            now_ms=now_ms,
            requested_environment=requested_environment,
            symbol=self.symbol,
            write_attempts_used=self._write_attempts,
            latency_status=latency_status,
            recovery_generation=recovery_generation,
            market_generation=market_generation,
            kill_switch_mode=kill_switch_mode,
        )

    def on_private_latency_event(
        self,
        *,
        result: LiveReadinessResult,
        provenance: ReadinessProvenance,
        now_ms: Milliseconds,
        environment: Environment,
    ) -> BootstrapActivation:
        """首个**可测量** private 事件到达后的强制升级/阻塞。

        - bootstrap 立即失效（无论结果如何）；
        - `LIVE_READY` + `HEALTHY` ⇒ 签发 NORMAL authority；
        - `BLOCKED`（UNHEALTHY / UNKNOWN）⇒ `BLOCKED`；
        - 仍 `BOOTSTRAP_ELIGIBLE`（UNOBSERVED）⇒ `BLOCKED`（**不得**升级 NORMAL，也**不得**自动第二次 bootstrap）。
        """
        self._bootstrap = None
        self._authority = None
        self._phase = BootstrapPhase.SUPERSEDED
        if result.status is LiveReadinessStatus.LIVE_READY:
            return self._issue_normal(result=result, provenance=provenance, now_ms=now_ms,
                                      environment=environment)
        if result.status is LiveReadinessStatus.BOOTSTRAP_ELIGIBLE:
            self._phase = BootstrapPhase.BLOCKED
            return BootstrapActivation(
                phase=BootstrapPhase.BLOCKED,
                reasons=(LiveReadinessReason.PRIVATE_LATENCY_UNOBSERVED.value,
                         "latency still UNOBSERVED after a private event: cannot upgrade to NORMAL"),
            )
        self._phase = BootstrapPhase.BLOCKED
        return BootstrapActivation(phase=BootstrapPhase.BLOCKED,
                                   reasons=tuple(r.value for r in result.reasons) or ("readiness BLOCKED",))

    # ------------------------------------------------------------------ 内部

    def _issue_normal(
        self,
        *,
        result: LiveReadinessResult,
        provenance: ReadinessProvenance,
        now_ms: Milliseconds,
        environment: Environment,
    ) -> BootstrapActivation:
        self._generation += 1
        try:
            authority = issue_authority(
                result,
                provenance=provenance,
                authority_id=f"{self.authority_id_prefix}-normal-{self.symbol}-{self._generation}",
                now_ms=now_ms,
                authority_ttl_ms=self.normal_ttl_ms,
                environment=environment,
            )
        except AuthorityError as exc:
            self._phase = BootstrapPhase.BLOCKED
            self._authority = None
            return BootstrapActivation(phase=BootstrapPhase.BLOCKED, reasons=(str(exc),))
        self._authority = authority
        self._phase = BootstrapPhase.NORMAL_READY
        return BootstrapActivation(phase=BootstrapPhase.NORMAL_READY, authority=authority)


@dataclass(slots=True)
class BootstrapWriteGate:
    """BOOTSTRAP 的**写边界闸门**（P0001.9.7.1 §一 / §二）。

    它只有一个职责：在**真正发出 REST 写请求之前**判定这张 bootstrap authority 是否仍然可用，
    并在**通过时立即消耗**那唯一一次 write attempt 额度。

    顺序不可颠倒：`authorize()` 返回 valid ⇒ 额度**已经**被消耗；此后无论网络层是 timeout、
    transport error 还是 UNKNOWN，**额度都不会恢复**（本类不提供任何恢复/退还入口）。
    因此第一笔若为 UNKNOWN，第二笔注定被 `ORDERS_USED_EXCEEDS_MAX` 拒绝。
    """

    coordinator: BootstrapAuthorityCoordinator

    def check(
        self,
        *,
        authority: BootstrapAuthority,
        now_ms: Milliseconds,
        requested_environment: Environment,
        symbol: str,
        notional_usdt: float,
        post_only: bool,
        private_continuity_valid: bool | None,
        latency_status: PrivateLatencyStatus,
        recovery_generation: object,
        market_generation: int,
        kill_switch_mode: KillSwitchMode,
    ) -> AuthorityVerdict:
        """**不消耗额度**的只读校验（供编排层决定"是否允许新增暴露"使用）。

        与 `authorize()` 的唯一区别：不调用 `record_write_attempt()`。
        真正的额度消耗只发生在写边界（`authorize()`），因此在编排层检查不会浪费那唯一一次额度。
        """
        if not isinstance(authority, BootstrapAuthority):
            raise AuthorityError("check() requires a BootstrapAuthority")
        if not isinstance(post_only, bool):
            raise AuthorityError("check() requires a bool post_only")
        if isinstance(notional_usdt, bool) or not isinstance(notional_usdt, (int, float)):
            raise AuthorityError("check() requires a numeric notional_usdt")

        reasons: list[AuthorityInvalidReason] = []
        details: list[str] = []
        if post_only is not True:
            reasons.append(AuthorityInvalidReason.POST_ONLY_REQUIRED)
            details.append("bootstrap only permits post-only quotes")
        if private_continuity_valid is not True:
            # JIT（P0001.9.7.1 §一）：collect 与写请求之间 continuity 必须仍然有效；
            # 未提供（None）也按无效处理——未知 ≠ 满足。
            reasons.append(AuthorityInvalidReason.PRIVATE_CONTINUITY_INVALID)
            details.append(
                f"private continuity is {private_continuity_valid!r} at the write boundary "
                "(a reconnect invalidates the collected readiness/authority)"
            )
        if float(notional_usdt) > float(authority.max_notional_usdt):
            reasons.append(AuthorityInvalidReason.NOTIONAL_EXCEEDS_MAX)
            details.append(
                f"notional {float(notional_usdt)} > max_notional_usdt {authority.max_notional_usdt}"
            )
        if reasons:
            return AuthorityVerdict(valid=False, reasons=tuple(reasons), details=tuple(details))
        return self.coordinator.validator.validate_bootstrap(
            authority,
            now_ms=now_ms,
            requested_environment=requested_environment,
            symbol=symbol,
            write_attempts_used=self.coordinator.write_attempts,
            latency_status=latency_status,
            recovery_generation=recovery_generation,  # type: ignore[arg-type]
            market_generation=market_generation,
            kill_switch_mode=kill_switch_mode,
        )

    def authorize(
        self,
        *,
        authority: BootstrapAuthority,
        now_ms: Milliseconds,
        requested_environment: Environment,
        symbol: str,
        notional_usdt: float,
        post_only: bool,
        private_continuity_valid: bool | None,
        latency_status: PrivateLatencyStatus,
        recovery_generation: object,
        market_generation: int,
        kill_switch_mode: KillSwitchMode,
    ) -> AuthorityVerdict:
        """校验 + 消耗额度。**valid 的返回値即意味着额度已被消耗**。"""
        verdict = self.check(
            authority=authority,
            now_ms=now_ms,
            requested_environment=requested_environment,
            symbol=symbol,
            notional_usdt=notional_usdt,
            post_only=post_only,
            private_continuity_valid=private_continuity_valid,
            latency_status=latency_status,
            recovery_generation=recovery_generation,
            market_generation=market_generation,
            kill_switch_mode=kill_switch_mode,
        )
        if verdict.valid:
            # 校验通过 ⇒ 在网络调用之前消耗额度（此后不会恢复）
            self.coordinator.record_write_attempt()
        return verdict


__all__ = [
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
