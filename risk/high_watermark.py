"""跨重启 / 跨 crash 的 **Equity High-Watermark**（P0001.9.4.2）。

语义（提案 §1）：

```text
drawdown = 自 trusted activation point 起，账户权益相对此后最高可信权益的回撤
```

**不是**"账户历史最高权益" —— activation 之前的历史 peak 未知是**可接受**的，但必须记录
`activation_ts` / `activation_equity`，不能把它描述成历史峰值。

关键不变量（提案 §6 / §7 / §8 / §10 / §11）：

1. **peak 单调**：`new_peak >= old_peak`，普通运行路径**绝不**降低 peak（只有显式 rebase 才换 epoch）；
2. **durable-before-publish**：新 peak 必须先落盘成功，才能成为 readiness/risk 可用的**已确认**状态；
3. **activation 显式**：第一次建立 HWM 只能由人类显式触发（满足前置条件），**绝不**自动发生；
4. **restart ≠ reset**：进程重启只加载旧 peak 继续同一 epoch；
5. **UTC 日界不重置**：drawdown 是 activation epoch 作用域，daily loss 才是日作用域；
6. **外部资金流 ⇒ INVALIDATED**：检测到 `TRANSFER` 等外部资本流后必须失效并要求显式 rebase
   （本阶段**不**实现 cash-flow-adjusted NAV）。

本模块是**纯领域逻辑**：不做 I/O（I/O 由 `storage.high_watermark` 的窄接口承担）、不依赖任何交易所细节。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from enum import Enum

from market.events.types import Milliseconds

#: 状态 schema 版本（由 store 持久化；不匹配即 fail closed）。
HIGH_WATERMARK_SCHEMA_VERSION = 1


class HighWatermarkError(Exception):
    """High-Watermark 契约错误（输入非法 / 状态非法）。"""


class HighWatermarkStatus(Enum):
    UNINITIALIZED = "UNINITIALIZED"
    ACTIVE = "ACTIVE"
    INVALIDATED = "INVALIDATED"


class HighWatermarkInvalidation(Enum):
    """失效原因类别（**typed**，避免让下游解析自由文本）。"""

    #: 检测到外部资本流（`TRANSFER` 等）⇒ 必须显式 rebase（提案 §11）
    CAPITAL_FLOW = "CAPITAL_FLOW"
    #: 其他失效（人工/运维原因）
    OTHER = "OTHER"


class HighWatermarkProblem(Enum):
    """证据侧的"为什么不可用"（readiness 据此给出精确 reason code，提案 §16）。"""

    STORE_FAILED = "STORE_FAILED"
    EQUITY_MISMATCH = "EQUITY_MISMATCH"
    CAPITAL_FLOW = "CAPITAL_FLOW"
    INVALID_OTHER = "INVALID_OTHER"


class HighWatermarkScope(Enum):
    """风险 epoch 的作用域（提案 §17：testnet / mainnet 状态绝不互相加载）。"""

    TESTNET = "TESTNET"
    MAINNET = "MAINNET"


@dataclass(frozen=True, slots=True)
class ActivationPreconditions:
    """显式 activation 必须全部满足的前置条件（提案 §2；不满足即拒绝）。"""

    recovery_recovered: bool
    position_flat: bool
    open_orders_zero: bool
    unresolved_orders_zero: bool
    daily_pnl_known: bool
    current_equity_known: bool
    account_snapshot_fresh: bool
    equity_consistent: bool
    scope: HighWatermarkScope
    deployment_id: str

    def __post_init__(self) -> None:
        for name in (
            "recovery_recovered",
            "position_flat",
            "open_orders_zero",
            "unresolved_orders_zero",
            "daily_pnl_known",
            "current_equity_known",
            "account_snapshot_fresh",
            "equity_consistent",
        ):
            value = getattr(self, name)
            if not isinstance(value, bool):
                raise HighWatermarkError(f"ActivationPreconditions.{name} must be a bool")
        if not isinstance(self.scope, HighWatermarkScope):
            raise HighWatermarkError("ActivationPreconditions.scope must be a HighWatermarkScope")
        if not isinstance(self.deployment_id, str) or not self.deployment_id:
            raise HighWatermarkError("ActivationPreconditions.deployment_id must be a non-empty string")

    @property
    def unmet(self) -> tuple[str, ...]:
        """未满足的条件名（用于 reason/detail；顺序固定便于断言）。"""
        problems: list[str] = []
        if not self.recovery_recovered:
            problems.append("recovery_not_recovered")
        if not self.position_flat:
            problems.append("position_not_flat")
        if not self.open_orders_zero:
            problems.append("open_orders_present")
        if not self.unresolved_orders_zero:
            problems.append("unresolved_orders_present")
        if not self.daily_pnl_known:
            problems.append("daily_pnl_unknown")
        if not self.current_equity_known:
            problems.append("equity_unknown")
        if not self.account_snapshot_fresh:
            problems.append("account_snapshot_stale")
        if not self.equity_consistent:
            problems.append("equity_mismatch")
        return tuple(problems)

    @property
    def satisfied(self) -> bool:
        return not self.unmet


@dataclass(frozen=True, slots=True)
class EquityHighWatermarkState:
    """durable 状态（提案 §4）。"""

    scope: HighWatermarkScope
    deployment_id: str
    activation_id: str
    activation_ts: Milliseconds
    activation_equity: float
    peak_equity: float
    peak_ts: Milliseconds
    last_equity: float
    last_observed_ts: Milliseconds
    capital_flow_checked_through: Milliseconds
    generation: int
    status: HighWatermarkStatus
    invalidation_reason: str = ""
    invalidation_kind: HighWatermarkInvalidation | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scope, HighWatermarkScope):
            raise HighWatermarkError("state.scope must be a HighWatermarkScope")
        if not isinstance(self.status, HighWatermarkStatus):
            raise HighWatermarkError("state.status must be a HighWatermarkStatus")
        for name in ("deployment_id", "activation_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise HighWatermarkError(f"state.{name} must be a non-empty string")
        for name in (
            "activation_ts",
            "peak_ts",
            "last_observed_ts",
            "capital_flow_checked_through",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise HighWatermarkError(f"state.{name} must be a non-negative int epoch-ms")
        for name in ("activation_equity", "peak_equity", "last_equity"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise HighWatermarkError(f"state.{name} must be a number")
            number = float(value)
            if not math.isfinite(number):
                raise HighWatermarkError(f"state.{name} must be finite")
            object.__setattr__(self, name, number)
        if isinstance(self.generation, bool) or not isinstance(self.generation, int) or self.generation < 0:
            raise HighWatermarkError("state.generation must be a non-negative int")
        if not isinstance(self.invalidation_reason, str):
            raise HighWatermarkError("state.invalidation_reason must be a string")
        if self.invalidation_kind is not None and not isinstance(self.invalidation_kind, HighWatermarkInvalidation):
            raise HighWatermarkError("state.invalidation_kind must be a HighWatermarkInvalidation or None")
        if self.status is HighWatermarkStatus.INVALIDATED and self.invalidation_kind is None:
            raise HighWatermarkError("INVALIDATED state must carry an invalidation_kind")
        # 单调性不变量：peak 不得低于 activation equity，也不得低于已观测 equity
        if self.peak_equity < self.activation_equity - 1e-9:
            raise HighWatermarkError("state.peak_equity must not be below activation_equity (monotonic invariant)")
        if self.peak_equity < self.last_equity - 1e-9:
            raise HighWatermarkError("state.peak_equity must not be below last_equity (monotonic invariant)")
        if self.peak_ts < self.activation_ts:
            raise HighWatermarkError("state.peak_ts must be >= activation_ts")
        if self.last_observed_ts < self.activation_ts:
            raise HighWatermarkError("state.last_observed_ts must be >= activation_ts")

    @property
    def active(self) -> bool:
        return self.status is HighWatermarkStatus.ACTIVE

    def drawdown(self, *, current_equity: float) -> float | None:
        """`max(0, peak - current)`；状态不 ACTIVE 时未知（None）。"""
        if not self.active:
            return None
        if isinstance(current_equity, bool) or not isinstance(current_equity, (int, float)):
            raise HighWatermarkError("current_equity must be a number")
        equity = float(current_equity)
        if not math.isfinite(equity):
            raise HighWatermarkError("current_equity must be finite")
        return max(0.0, self.peak_equity - equity)

    def drawdown_pct(self, *, current_equity: float) -> float | None:
        """`drawdown / peak`；peak <= 0 时返回 None（无法定义比率）。"""
        drawdown = self.drawdown(current_equity=current_equity)
        if drawdown is None or self.peak_equity <= 0.0:
            return None
        return drawdown / self.peak_equity


@dataclass(frozen=True, slots=True)
class HighWatermarkEvidence:
    """给 RiskSnapshot / readiness 消费的**已确认**证据（提案 §15 / §16）。"""

    status: HighWatermarkStatus
    peak_equity: float | None
    peak_ts: Milliseconds | None
    activation_id: str | None
    activation_ts: Milliseconds | None
    activation_equity: float | None
    generation: int
    scope: HighWatermarkScope | None
    detail: str = ""
    problem: HighWatermarkProblem | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, HighWatermarkStatus):
            raise HighWatermarkError("evidence.status must be a HighWatermarkStatus")
        if not isinstance(self.generation, int) or isinstance(self.generation, bool) or self.generation < 0:
            raise HighWatermarkError("evidence.generation must be a non-negative int")
        if self.problem is not None and not isinstance(self.problem, HighWatermarkProblem):
            raise HighWatermarkError("evidence.problem must be a HighWatermarkProblem or None")

    @classmethod
    def uninitialized(cls, *, detail: str = "high-watermark has never been activated") -> "HighWatermarkEvidence":
        """显式表达"从未 activation"（**不是**默认值：调用方必须显式使用）。"""
        return cls(
            status=HighWatermarkStatus.UNINITIALIZED,
            peak_equity=None,
            peak_ts=None,
            activation_id=None,
            activation_ts=None,
            activation_equity=None,
            generation=0,
            scope=None,
            detail=detail,
        )

    @classmethod
    def store_failed(cls, *, detail: str) -> "HighWatermarkEvidence":
        """durable write 失败 ⇒ 不得当作可用（提案 §5 / SC-7）。"""
        return cls(
            status=HighWatermarkStatus.UNINITIALIZED,
            peak_equity=None,
            peak_ts=None,
            activation_id=None,
            activation_ts=None,
            activation_equity=None,
            generation=0,
            scope=None,
            detail=detail,
            problem=HighWatermarkProblem.STORE_FAILED,
        )

    @classmethod
    def equity_mismatch(cls, *, detail: str) -> "HighWatermarkEvidence":
        """本地 accounting equity 与交易所权威 equity 偏差超容差（提案 §13）。"""
        return cls(
            status=HighWatermarkStatus.UNINITIALIZED,
            peak_equity=None,
            peak_ts=None,
            activation_id=None,
            activation_ts=None,
            activation_equity=None,
            generation=0,
            scope=None,
            detail=detail,
            problem=HighWatermarkProblem.EQUITY_MISMATCH,
        )

    @classmethod
    def from_state(cls, state: EquityHighWatermarkState) -> "HighWatermarkEvidence":
        if not isinstance(state, EquityHighWatermarkState):
            raise HighWatermarkError("from_state requires an EquityHighWatermarkState")
        if not state.active:
            problem = (
                HighWatermarkProblem.CAPITAL_FLOW
                if state.invalidation_kind is HighWatermarkInvalidation.CAPITAL_FLOW
                else HighWatermarkProblem.INVALID_OTHER
            )
            return cls(
                status=state.status,
                peak_equity=None,
                peak_ts=None,
                activation_id=state.activation_id,
                activation_ts=state.activation_ts,
                activation_equity=state.activation_equity,
                generation=state.generation,
                scope=state.scope,
                detail=state.invalidation_reason,
                problem=problem,
            )
        return cls(
            status=state.status,
            peak_equity=state.peak_equity,
            peak_ts=state.peak_ts,
            activation_id=state.activation_id,
            activation_ts=state.activation_ts,
            activation_equity=state.activation_equity,
            generation=state.generation,
            scope=state.scope,
        )

    @property
    def drawdown_known(self) -> bool:
        return self.status is HighWatermarkStatus.ACTIVE and self.peak_equity is not None

    def equity_observation(self) -> tuple[float, Milliseconds] | None:
        """RiskSnapshot 需要的 `(peak_equity, peak_ts)`；不可用时 None。"""
        if not self.drawdown_known or self.peak_ts is None:
            return None
        return (float(self.peak_equity), self.peak_ts)  # type: ignore[arg-type]


def state_to_mapping(state: EquityHighWatermarkState) -> dict[str, object]:
    """序列化（canonical、无浮点伪精度问题：直接写 float 的 repr）。"""
    if not isinstance(state, EquityHighWatermarkState):
        raise HighWatermarkError("state_to_mapping requires an EquityHighWatermarkState")
    return {
        "scope": state.scope.value,
        "deployment_id": state.deployment_id,
        "activation_id": state.activation_id,
        "activation_ts": state.activation_ts,
        "activation_equity": state.activation_equity,
        "peak_equity": state.peak_equity,
        "peak_ts": state.peak_ts,
        "last_equity": state.last_equity,
        "last_observed_ts": state.last_observed_ts,
        "capital_flow_checked_through": state.capital_flow_checked_through,
        "generation": state.generation,
        "status": state.status.value,
        "invalidation_reason": state.invalidation_reason,
        "invalidation_kind": None if state.invalidation_kind is None else state.invalidation_kind.value,
    }


def state_from_mapping(raw: dict[str, object]) -> EquityHighWatermarkState:
    """反序列化（严格：缺字段 / 类型错 / 取值非法都抛错）。"""
    if not isinstance(raw, dict):
        raise HighWatermarkError("state payload must be a JSON object")
    required = (
        "scope",
        "deployment_id",
        "activation_id",
        "activation_ts",
        "activation_equity",
        "peak_equity",
        "peak_ts",
        "last_equity",
        "last_observed_ts",
        "capital_flow_checked_through",
        "generation",
        "status",
    )
    for name in required:
        if name not in raw:
            raise HighWatermarkError(f"state payload missing field {name!r}")
    try:
        scope = HighWatermarkScope(str(raw["scope"]))
    except ValueError:
        raise HighWatermarkError(f"state payload has unknown scope {raw['scope']!r}") from None
    try:
        status = HighWatermarkStatus(str(raw["status"]))
    except ValueError:
        raise HighWatermarkError(f"state payload has unknown status {raw['status']!r}") from None
    try:
        return EquityHighWatermarkState(
            scope=scope,
            deployment_id=str(raw["deployment_id"]),
            activation_id=str(raw["activation_id"]),
            activation_ts=int(raw["activation_ts"]),  # type: ignore[arg-type]
            activation_equity=float(raw["activation_equity"]),  # type: ignore[arg-type]
            peak_equity=float(raw["peak_equity"]),  # type: ignore[arg-type]
            peak_ts=int(raw["peak_ts"]),  # type: ignore[arg-type]
            last_equity=float(raw["last_equity"]),  # type: ignore[arg-type]
            last_observed_ts=int(raw["last_observed_ts"]),  # type: ignore[arg-type]
            capital_flow_checked_through=int(raw["capital_flow_checked_through"]),  # type: ignore[arg-type]
            generation=int(raw["generation"]),  # type: ignore[arg-type]
            status=status,
            invalidation_reason=str(raw.get("invalidation_reason", "") or ""),
            invalidation_kind=_optional_invalidation(raw.get("invalidation_kind")),
        )
    except (TypeError, ValueError) as exc:
        # 任何字段类型/取值问题都收敛为本领域的错误（调用方据此 BLOCKED），绝不外泄原始异常
        raise HighWatermarkError(f"state payload field is invalid: {type(exc).__name__}") from None


class HighWatermarkTracker:
    """epoch 状态的唯一 Owner（提案 §6 / §7：peak 单调 + durable-before-publish）。"""

    def __init__(self, *, state: EquityHighWatermarkState | None, store: object) -> None:
        """`state=None` ⇒ 从未 activation（UNINITIALIZED）；`store` 需实现 `save(state)`。"""
        save = getattr(store, "save", None)
        if not callable(save):
            raise HighWatermarkError("HighWatermarkTracker requires a store exposing save(state)")
        self._store = store
        self._state = state

    # ------------------------------------------------------------------ 只读

    @property
    def state(self) -> EquityHighWatermarkState | None:
        return self._state

    @property
    def status(self) -> HighWatermarkStatus:
        return HighWatermarkStatus.UNINITIALIZED if self._state is None else self._state.status

    def evidence(self) -> HighWatermarkEvidence:
        """当前**已确认**（已持久化）的证据。"""
        if self._state is None:
            return HighWatermarkEvidence.uninitialized()
        return HighWatermarkEvidence.from_state(self._state)

    # ------------------------------------------------------------------ activation / rebase

    def activate(
        self,
        *,
        preconditions: ActivationPreconditions,
        current_equity: float,
        activation_id: str,
        ts: Milliseconds,
        reason: str = "",
    ) -> EquityHighWatermarkState:
        """**显式**建立新的风险 epoch（提案 §2 / §9）。

        - 前置条件必须全部满足（flat / 0 挂单 / RECOVERED / equity 已知 / 快照新鲜 / equity 一致）；
        - 已有状态时：只有 INVALIDATED 或**显式 rebase**（调用方给出新 `activation_id` 与 reason）才允许；
          直接对 ACTIVE 状态重复 activate 会抛错（禁止靠"再来一次"洗掉 peak）。
        """
        if not isinstance(preconditions, ActivationPreconditions):
            raise HighWatermarkError("activate() requires ActivationPreconditions")
        unimplemented = preconditions.unmet
        if unimplemented:
            raise HighWatermarkError("activation preconditions unmet: " + ", ".join(unimplemented))
        equity = _require_equity(current_equity)
        if not isinstance(activation_id, str) or not activation_id:
            raise HighWatermarkError("activation_id must be a non-empty string")
        if isinstance(ts, bool) or not isinstance(ts, int) or ts < 0:
            raise HighWatermarkError("activation ts must be a non-negative int epoch-ms")
        if self._state is not None and self._state.active:
            raise HighWatermarkError(
                "high-watermark is already ACTIVE for this epoch; "
                "use rebase() (explicit, human-authorized) to start a new epoch"
            )
        if self._state is not None and self._state.activation_id == activation_id:
            raise HighWatermarkError("activation_id must be new for a rebase (must not reuse the previous epoch id)")
        previous_generation = 0 if self._state is None else self._state.generation
        state = EquityHighWatermarkState(
            scope=preconditions.scope,
            deployment_id=preconditions.deployment_id,
            activation_id=activation_id,
            activation_ts=ts,
            activation_equity=equity,
            peak_equity=equity,
            peak_ts=ts,
            last_equity=equity,
            last_observed_ts=ts,
            capital_flow_checked_through=ts,
            generation=previous_generation + 1,
            status=HighWatermarkStatus.ACTIVE,
            invalidation_reason=reason,
        )
        self._store.save(state)  # durable-before-publish（§7）
        self._state = state
        return state

    def rebase(self, **kwargs: object) -> EquityHighWatermarkState:
        """`activate()` 的显式别名：语义上强调"人类授权的重置"（提案 §9）。"""
        return self.activate(**kwargs)  # type: ignore[arg-type]

    def invalidate_for_capital_flow(
        self,
        *,
        reason: str,
        ts: Milliseconds,
        capital_flow_checked_through: Milliseconds | None = None,
        kind: HighWatermarkInvalidation = HighWatermarkInvalidation.CAPITAL_FLOW,
    ) -> EquityHighWatermarkState:
        """外部资金流（`TRANSFER` 等）⇒ INVALIDATED，并要求显式 rebase（提案 §11）。"""
        if self._state is None:
            raise HighWatermarkError("cannot invalidate a high-watermark that was never activated")
        if not isinstance(reason, str) or not reason:
            raise HighWatermarkError("invalidate_for_capital_flow requires a non-empty reason")
        if not isinstance(kind, HighWatermarkInvalidation):
            raise HighWatermarkError("invalidate_for_capital_flow kind must be a HighWatermarkInvalidation")
        checked = self._state.capital_flow_checked_through if capital_flow_checked_through is None else (
            int(capital_flow_checked_through)
        )
        state = replace(
            self._state,
            status=HighWatermarkStatus.INVALIDATED,
            invalidation_reason=reason,
            capital_flow_checked_through=checked,
            invalidation_kind=kind,
        )
        self._store.save(state)
        self._state = state
        return state

    # ------------------------------------------------------------------ 观测

    def observe_equity(self, *, current_equity: float, ts: Milliseconds) -> EquityHighWatermarkState:
        """观测一次 equity（提案 §6 / §7 / §14）。

        只有 `current_equity > peak` 才提升 peak；提升必须**先 durable 成功再发布**。
        """
        if self._state is None:
            raise HighWatermarkError("equity observations require an activated high-watermark")
        if not self._state.active:
            raise HighWatermarkError(
                f"high-watermark is {self._state.status.value}; explicit rebase required before further observations"
            )
        equity = _require_equity(current_equity)
        if isinstance(ts, bool) or not isinstance(ts, int) or ts < 0:
            raise HighWatermarkError("observation ts must be a non-negative int epoch-ms")
        if ts < self._state.activation_ts:
            raise HighWatermarkError("observation ts must not precede activation_ts")
        if equity > self._state.peak_equity + 1e-9:
            candidate = replace(
                self._state, peak_equity=equity, peak_ts=ts, last_equity=equity, last_observed_ts=ts
            )
            self._store.save(candidate)  # 先持久化，再发布
            self._state = candidate
            return candidate
        # peak 不下降（普通运行路径绝不降低 peak）；只更新 last 观测值
        updated = self._state
        if ts >= self._state.last_observed_ts:
            updated = replace(self._state, last_equity=equity, last_observed_ts=ts)
            self._store.save(updated)
        self._state = updated
        return updated


def _optional_invalidation(value: object) -> HighWatermarkInvalidation | None:
    if value is None or value == "":
        return None
    try:
        return HighWatermarkInvalidation(str(value))
    except ValueError:
        raise HighWatermarkError(f"state payload has unknown invalidation_kind {value!r}") from None


def _require_equity(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HighWatermarkError("equity must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise HighWatermarkError("equity must be finite")
    return number


__all__ = [
    "HIGH_WATERMARK_SCHEMA_VERSION",
    "ActivationPreconditions",
    "HighWatermarkInvalidation",
    "HighWatermarkProblem",
    "EquityHighWatermarkState",
    "HighWatermarkError",
    "HighWatermarkEvidence",
    "HighWatermarkScope",
    "HighWatermarkStatus",
    "HighWatermarkTracker",
    "state_from_mapping",
    "state_to_mapping",
]
