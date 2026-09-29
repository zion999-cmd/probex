"""`LiveExecutionOrchestrator`：Testnet live loop 的唯一编排 Owner（P0001.9.7 §1 – §19）。

职责**仅限**：收集本轮输入 → 调 `MakerPolicy` → 把 `QuoteAction` 映射到既有 `ExecutionEngine` →
驱动 poll / user-stream 桥接 → 处理 authority 失效与 reconciliation 触发 → 输出 telemetry。

它**不**算价格 / 不算 size / 不算风险 / 不生成 prediction / 不解释 Jev；
也不修改 `MakerPolicy`、`RiskGate`、`OrderTracker` 或 adapter 契约。

关键不变量：

| 编号 | 不变量 |
| --- | --- |
| §3 | `PLACE` ⇒ `engine.submit`；`KEEP` ⇒ 什么都不做；`CANCEL` ⇒ `engine.cancel`；`REPLACE` ⇒ **cancel-before-replace**（必须等旧单终态才补挂） |
| §4 | 每侧最多一个 active quote；发现同侧多单 ⇒ `ORDER_MULTIPLICITY_VIOLATION` ⇒ 停止新增 + 触发 reconciliation（**不擅自取舍**） |
| §5 | `existing_orders` **只**来自 `OrderManager.active_orders`（Tracker 是生命周期 Owner；REST 只用于 reconciliation） |
| §8 | 每次新增 submit 前校验当前 authority；失效 ⇒ 不 submit（但 **cancel 仍执行**） |
| §9–§11 | 不额外解释 prediction / market / exposure；只消费既有 MakerPolicy 输出，并在必要时触发 reconciliation |
| §12 | user stream 事实**只**经 `adapter.bridge_user_event()` → `adapter.poll()` → `engine.on_events()` |
| §17 | `stop()`：禁新增 → 撤 Probex 挂单 → drain → 确认 0 挂单 → 停 runtime；**绝不**自动市价平仓，残留持仓只报告 `POSITION_REMAINS` |
| §18 | 未完成 Recovery / Readiness 之前，submit 恒为 0（由 authority + `OrchestratorState` 共同保证） |
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Protocol, Sequence

from market.events.types import Milliseconds, Venue
from market.state.types import MarketState
from readiness.authority import AuthorityInvalidReason, ExecutionReadinessAuthorityValidator
from readiness.bootstrap import BootstrapAuthority, BootstrapWriteGate
from readiness.types import Environment, PrivateLatencyStatus, RecoveryGeneration
from risk.snapshot import build_risk_snapshot
from risk.types import KillSwitchMode
from strategy.maker.types import MakerDecision, QuoteAction

from connectors.binance.execution.adapter import (
    BinanceExecutionAdapter,
    ExecutionAuthorityContext,
    ExternalFactsUnavailableError,
    SubmitClassification,
)
from connectors.binance.private.orders import is_probex_order
from execution.engine import ExecutionEngine
from execution.events import ExecutionEvent
from execution.types import Order, OrderStatus
from live.telemetry import LoopTelemetry, LoopTelemetrySink
from portfolio.types import Side


class OrchestratorError(Exception):
    """编排契约错误。"""


class ExecutionDisabledError(OrchestratorError):
    """在 `execution_enabled=False`（observe-only）下尝试写操作。"""


class OrderMultiplicityViolation(OrchestratorError):
    """同一侧出现 >1 active order（§4）：fail closed，停止新增并交给 reconciliation。"""


class LoopPhase(Enum):
    """一轮 loop 的阶段（固定顺序，§2）。"""

    DRAIN_EVENTS = "drain_events"
    BUILD_SNAPSHOT = "build_snapshot"
    COLLECT_INPUTS = "collect_inputs"
    DECIDE = "decide"
    EXECUTE = "execute"
    DRAIN_EXECUTION = "drain_execution"
    RECORD = "record"


class OrchestratorState(Enum):
    """编排状态（是否允许新增暴露）。"""

    #: 未完成 recovery/readiness：只读、零 submit。
    NOT_READY = "NOT_READY"
    OBSERVE_ONLY = "OBSERVE_ONLY"
    #: 允许写（仅 Testnet）。
    LIVE = "LIVE"
    #: 需要 reconciliation（未知事实/多单/断线）；在收敛前禁止新增暴露。
    RECONCILING = "RECONCILING"
    STOPPED = "STOPPED"


@dataclass(frozen=True, slots=True)
class ReconciliationOutcome:
    """一次 reconciliation 的结果。"""

    triggered_by: str
    converged: bool
    detail: str = ""
    unresolved_orders: int = 0


@dataclass(frozen=True, slots=True)
class StopReport:
    """`stop()` 的事实（§17：**绝不**自动平仓）。"""

    cancelled: tuple[str, ...]
    remaining_active: tuple[str, ...]
    position_qty: float
    position_remains: bool
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LoopOutcome:
    """一轮 loop 的结果。"""

    telemetry: LoopTelemetry
    submitted: tuple[Order, ...] = ()
    cancelled: tuple[str, ...] = ()
    replaced: tuple[str, ...] = ()
    reconciliation: ReconciliationOutcome | None = None


@dataclass(frozen=True, slots=True)
class OrchestratorConfig:
    """编排配置（**全部显式**，无默认业务值）。"""

    symbol: str
    environment: Environment
    #: observe-only：为 True 时任何写操作都会被拒（§19 Phase A）
    execution_enabled: bool
    #: 是否允许写（与 environment 强绑定：Testnet 才能 True）
    allow_write: bool

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol:
            raise OrchestratorError("OrchestratorConfig.symbol must be a non-empty string")
        if not isinstance(self.environment, Environment):
            raise OrchestratorError("OrchestratorConfig.environment must be an Environment")
        if not isinstance(self.execution_enabled, bool) or not isinstance(self.allow_write, bool):
            raise OrchestratorError("execution_enabled / allow_write must be bool")
        if self.execution_enabled and self.allow_write and self.environment is not Environment.TESTNET:
            raise OrchestratorError("write-enabled orchestration is only allowed on TESTNET (P0001.9.7 scope)")


@dataclass
class LiveExecutionOrchestrator:
    """把 MakerPolicy 决策稳定地转化为真实（Testnet）订单生命周期。"""

    config: OrchestratorConfig
    engine: ExecutionEngine
    adapter: BinanceExecutionAdapter
    policy: object  # MakerPolicy（duck-typed：只需 decide(...)）
    authority_provider: Callable[[], ExecutionAuthorityContext]
    #: 本轮可用于**新增暴露**的 notional 预算（由调用方/风险配置注入；返回 None = 未知 ⇒ 策略 fail closed）。
    #: orchestrator 只透传，不做风险数学（§1）。
    risk_budget_provider: Callable[[object], float | None]
    authority_validator: ExecutionReadinessAuthorityValidator = field(
        default_factory=ExecutionReadinessAuthorityValidator
    )
    clock: Callable[[], Milliseconds] = field(default=lambda: 0)  # type: ignore[assignment]
    telemetry: LoopTelemetrySink = field(default_factory=LoopTelemetrySink)
    state: OrchestratorState = OrchestratorState.NOT_READY
    #: 最近一次 reconciliation 结果（供 telemetry）
    last_reconciliation: ReconciliationOutcome | None = None
    #: REPLACE 的待补挂队列（client_order_id → (side, proposal 供给)）
    _pending_replace: dict[str, object] = field(default_factory=dict)
    _violations: int = 0
    _startup_ready: bool = False

    def __post_init__(self) -> None:
        if not callable(self.clock):
            raise OrchestratorError("LiveExecutionOrchestrator.clock must be injected (no default time source)")
        if not hasattr(self.policy, "decide"):
            raise OrchestratorError("LiveExecutionOrchestrator.policy must expose decide(...)")
        if not callable(self.risk_budget_provider):
            raise OrchestratorError("LiveExecutionOrchestrator.risk_budget_provider must be callable")
        if self.state is OrchestratorState.LIVE and not self.config.execution_enabled:
            raise OrchestratorError("LIVE state requires execution_enabled")

    # ------------------------------------------------------------------ 状态

    def mark_ready(self) -> None:
        """Recovery + Readiness + authority 就绪后由调用方显式调用（§18/§20）。"""
        self._startup_ready = True
        self.state = (
            OrchestratorState.LIVE if self.config.execution_enabled and self.config.allow_write
            else OrchestratorState.OBSERVE_ONLY
        )

    def mark_not_ready(self) -> None:
        """断线 / 失效 ⇒ 回到 NOT_READY（在重新 recovery+readiness 之前零 submit）。"""
        self._startup_ready = False
        self.state = OrchestratorState.NOT_READY

    @property
    def multiplicity_violations(self) -> int:
        return self._violations

    # ------------------------------------------------------------------ 主循环

    def run_loop(
        self,
        *,
        state: MarketState | None,
        prediction: object | None,
        position: object | None = None,
        kill_switch: KillSwitchMode = KillSwitchMode.NORMAL,
    ) -> LoopOutcome:
        """执行**一轮**完整 loop（顺序固定，§2）。"""
        loop_ts = int(self.clock())
        notes: list[str] = []

        # 1) ingest / poll external events（engine.poll 返回 ExecutionResult）
        self.engine.poll(now_ms=loop_ts)
        # 2) update Tracker / Accounting + user stream 桥接（事实翻译边界在 adapter）
        self._drain_user_stream()

        # 3) fresh RiskSnapshot（在决策**之前**构建）
        snapshot = self.engine.snapshot(self.config.symbol, now_ms=loop_ts)
        # position 是**派生事实**：默认取 accounting 的当前持仓；显式传入时必须与快照一致（fail closed）
        derived_position = self.engine.accounting.position(self.config.symbol)
        if position is None:
            position = derived_position
        elif abs(float(getattr(position, "qty", 0.0)) - derived_position.qty) > 1e-9:
            raise OrchestratorError(
                "run_loop position argument is inconsistent with accounting "
                f"({getattr(position, 'qty', None)} vs {derived_position.qty}); pass position=None to derive"
            )

        # 5) collect latest inputs（market/prediction 由调用方给出）
        market_healthy = bool(getattr(getattr(state, "quality", None), "tradeable", False)) if state else False

        # 4/6) decide
        existing = self._existing_orders()
        multiplicity = self._check_multiplicity(existing)
        if multiplicity is not None:
            notes.append(multiplicity)
            reconciliation = self._reconcile(triggered_by="ORDER_MULTIPLICITY_VIOLATION", now_ms=loop_ts)
            telemetry = self._record(
                loop_ts=loop_ts, state=state, prediction=prediction, snapshot=snapshot,
                decision=None, notes=notes, reconciliation=reconciliation,
            )
            return LoopOutcome(telemetry=telemetry, reconciliation=reconciliation)

        decision: MakerDecision | None = None
        if state is not None:
            decision = self.policy.decide(  # type: ignore[attr-defined]
                state=state,
                prediction=prediction,
                position=position,
                snapshot=snapshot,
                existing_orders=existing,
                remaining_risk_budget=self.risk_budget_provider(snapshot),
                kill_switch=kill_switch,
            )

        # 7) 执行动作
        submitted: list[Order] = []
        cancelled: list[str] = []
        replaced: list[str] = []
        risk_rejects = 0
        unknown_submits = 0
        unknown_cancels = 0
        if decision is not None:
            for side_decision in (decision.bid, decision.ask):
                if side_decision.action is QuoteAction.KEEP or side_decision.action is QuoteAction.NONE:
                    continue
                if side_decision.action is QuoteAction.CANCEL:
                    cancelled.extend(self._cancel(side_decision, now_ms=loop_ts, notes=notes))
                elif side_decision.action is QuoteAction.REPLACE:
                    # §3：cancel-before-replace —— 旧单未终态前**不**补挂
                    done = self._cancel(side_decision, now_ms=loop_ts, notes=notes)
                    cancelled.extend(done)
                    if done:
                        replaced.extend(done)
                        self._pending_replace[side_decision.client_order_id or ""] = side_decision
                elif side_decision.action is QuoteAction.PLACE:
                    outcome = self._place(side_decision, now_ms=loop_ts, notes=notes)
                    if outcome is not None and outcome.submitted:
                        submitted.append(outcome.order)  # type: ignore[arg-type]
                    elif outcome is not None and outcome.rejected:
                        risk_rejects += 1
        # 补挂：只在旧单**已确认终态**后执行（cancel-before-replace 的第二步）
        for client_order_id, pending in list(self._pending_replace.items()):
            order = self.engine.manager.tracker.order(client_order_id)
            if order is None or not order.is_terminal:
                continue
            self._pending_replace.pop(client_order_id, None)
            outcome = self._place(pending, now_ms=loop_ts, notes=notes)  # type: ignore[arg-type]
            if outcome is not None and outcome.submitted:
                submitted.append(outcome.order)  # type: ignore[arg-type]

        # 8) 再 poll 一次 execution events
        self.engine.poll(now_ms=int(self.clock()))

        # 9) 记录
        telemetry = self._record(
            loop_ts=loop_ts, state=state, prediction=prediction, snapshot=snapshot,
            decision=decision, notes=notes, reconciliation=None,
            submitted=len(submitted), cancelled=len(cancelled), replaced=len(replaced),
            risk_rejects=risk_rejects, unknown_submits=unknown_submits, unknown_cancels=unknown_cancels,
        )
        return LoopOutcome(
            telemetry=telemetry,
            submitted=tuple(submitted),
            cancelled=tuple(cancelled),
            replaced=tuple(replaced),
        )

    # ------------------------------------------------------------------ 动作

    def _place(self, side_decision: object, *, now_ms: Milliseconds, notes: list[str]):
        if not self._can_increase_exposure(notes=notes):
            return None
        proposal = getattr(side_decision, "proposal", None)
        if proposal is None:
            notes.append("place_without_proposal")
            return None
        try:
            self._require_write_allowed(notes=notes)
        except ExecutionDisabledError:
            # observe-only / 未授权写：**记录并跳过**（loop 必须继续稳定运行，不抛出）
            return None
        outcome = self.engine.submit(proposal, now_ms=now_ms)
        if outcome.rejected and outcome.rejection is not None:
            reason = outcome.rejection.decision.reason_code
            notes.append(f"risk_reject:{getattr(reason, 'value', reason)}")
        return outcome

    def _cancel(self, side_decision: object, *, now_ms: Milliseconds, notes: list[str]) -> list[str]:
        """撤单是降险动作：**不要求** authority / LIVE（§8）。"""
        client_order_id = getattr(side_decision, "client_order_id", None)
        if not client_order_id:
            notes.append("cancel_without_client_order_id")
            return []
        order = self.engine.manager.tracker.order(client_order_id)
        if order is None:
            notes.append(f"cancel_unknown_order:{client_order_id}")
            return []
        if order.is_terminal:
            return []
        self.engine.cancel(client_order_id, now_ms=now_ms)
        # §12 / D-021：只有**确认终态**才算撤单完成；否则记录为 pending（由 query/user stream 收敛）
        after = self.engine.manager.tracker.order(client_order_id)
        if after is not None and after.status is OrderStatus.CANCELED:
            return [client_order_id]
        notes.append(f"cancel_pending:{client_order_id}")
        return []

    def _can_increase_exposure(self, *, notes: list[str]) -> bool:
        """§8 / §11 / §18：状态、authority、不确定暴露三道门。"""
        if not self._startup_ready or self.state is OrchestratorState.NOT_READY:
            notes.append("not_ready:no_submit_before_recovery_and_readiness")
            return False
        if self.state is OrchestratorState.RECONCILING:
            notes.append("reconciling:no_new_exposure")
            return False
        if self.engine.manager.tracker.has_unknown_exposure:
            notes.append("unknown_exposure:no_new_exposure")
            return False
        if self.engine.manager.tracker.uncertain_exposure() > 0.0:
            notes.append("uncertain_exposure:no_new_exposure")
            return False
        verdict = self._authority_verdict()
        if not verdict.valid:
            notes.append(f"authority_invalid:{verdict.reasons[0].value}")
            return False
        return True

    def _authority_verdict(self):
        """写前置的 authority 判定（分派 kind）。

        P0001.9.7.1：BOOTSTRAP authority 走 `BootstrapWriteGate.check(...)`——**只读校验，不消耗额度**；
        额度只在写边界（adapter 的 `authorize()`）消耗。因此：

        - 第一笔之前：bootstrap 有效 ⇒ 允许新增暴露；
        - 额度耗尽 / 已 superseded ⇒ 此门返回 invalid ⇒ `_can_increase_exposure` 为 False
          ⇒ PLACE / REPLACE（需新写请求）被拒；**纯 CANCEL 不受影响**（降低风险路径不经过此门）。
        """
        context = self.authority_provider()
        authority = context.authority
        if authority is None:
            return _InvalidVerdict(AuthorityInvalidReason.NOT_LIVE_READY, "no authority")
        if isinstance(authority, BootstrapAuthority):
            gate: BootstrapWriteGate | None = context.bootstrap_gate
            if gate is None:
                return _InvalidVerdict(
                    AuthorityInvalidReason.BOOTSTRAP_SUPERSEDED, "bootstrap authority without a write gate"
                )
            latency = (
                PrivateLatencyStatus.UNKNOWN
                if context.latency_status is None
                else context.latency_status
            )
            return gate.check(
                authority=authority,
                now_ms=int(self.clock()),
                requested_environment=self.config.environment,
                symbol=self.config.symbol,
                # 编排层不预判单笔 notional（那是写边界的事）；用该授权自身上限做上限检查
                notional_usdt=float(authority.max_notional_usdt),
                post_only=True,          # 报价只能 post-only；写边界会再校一次
                private_continuity_valid=context.private_continuity_valid,
                latency_status=latency,
                recovery_generation=context.recovery_generation,
                market_generation=context.market_generation,
                kill_switch_mode=context.kill_switch_mode,
            )
        return self.authority_validator.validate(
            authority,
            now_ms=int(self.clock()),
            requested_environment=self.config.environment,
            recovery_generation=context.recovery_generation,
            market_generation=context.market_generation,
            hwm_activation_id=context.hwm_activation_id,
            hwm_generation=context.hwm_generation,
            kill_switch_mode=context.kill_switch_mode,
        )

    def _require_write_allowed(self, *, notes: list[str]) -> None:
        """写前置条件（供直接调用方复用）；orchestrator 内部会把它转成 note + 跳过。"""
        if not self.config.execution_enabled:
            notes.append("execution_disabled:observe_only")
            raise ExecutionDisabledError("write attempted while execution is disabled (observe-only)")
        if not self.config.allow_write:
            notes.append("write_not_allowed")
            raise ExecutionDisabledError("write attempted while allow_write is False")

    # ------------------------------------------------------------------ Tracker / stream

    def _existing_orders(self) -> tuple[Order, ...]:
        """§5：**只**来自 Tracker（不喂 REST openOrders）。"""
        return tuple(
            order for order in self.engine.manager.active_orders if order.symbol == self.config.symbol
        )

    def _check_multiplicity(self, orders: Sequence[Order]) -> str | None:
        for side in (Side.BUY, Side.SELL):
            side_orders = [order for order in orders if order.side is side]
            if len(side_orders) > 1:
                self._violations += 1
                self.state = OrchestratorState.RECONCILING
                return f"ORDER_MULTIPLICITY_VIOLATION:{side.value}:{len(side_orders)}"
        return None

    def _drain_user_stream(self) -> tuple[ExecutionEvent, ...]:
        """§12：user stream 事实只经 adapter bridge → poll → engine.on_events。"""
        events: tuple[ExecutionEvent, ...] = tuple(self.adapter.poll())
        if not events:
            return ()
        self.engine.on_events(events, now_ms=int(self.clock()))
        return events

    def bridge_stream_event(self, observation: object) -> tuple[ExecutionEvent, ...]:
        """由 live runner 在 pump private stream 时调用（事实翻译边界在 adapter）。"""
        return self.adapter.bridge_user_event(observation)  # type: ignore[arg-type]

    # ------------------------------------------------------------------ Reconciliation

    def _reconcile(self, *, triggered_by: str, now_ms: Milliseconds) -> ReconciliationOutcome:
        """§7：只在明确触发条件下做，并在收敛前禁止新增暴露。"""
        from execution.reconciliation import reconcile

        self.state = OrchestratorState.RECONCILING
        try:
            external_orders = self.adapter.open_orders()
            external_fills = self.adapter.recent_fills()
        except ExternalFactsUnavailableError as exc:
            outcome = ReconciliationOutcome(
                triggered_by=triggered_by, converged=False, detail=f"external facts unavailable: {exc.detail}"
            )
            self.last_reconciliation = outcome
            return outcome
        report = reconcile(
            self.engine.manager.tracker,
            external_open_orders=external_orders,
            external_recent_fills=external_fills,
            timestamp=now_ms,
            venue=Venue.BINANCE,
        )
        unresolved = self.engine.manager.tracker.unresolved_orders()
        converged = report.converged or not report.corrective_actions
        unresolved_only = not unresolved
        outcome = ReconciliationOutcome(
            triggered_by=triggered_by,
            converged=bool(converged and unresolved_only),
            detail="; ".join(sorted({action.kind.value for action in report.actions})),
            unresolved_orders=len(unresolved),
        )
        self.last_reconciliation = outcome
        if outcome.converged:
            self.state = (
                OrchestratorState.LIVE if self.config.execution_enabled and self.config.allow_write
                else OrchestratorState.OBSERVE_ONLY
            )
        return outcome

    def reconcile_now(self, *, reason: str) -> ReconciliationOutcome:
        """外部显式触发（submit/cancel UNKNOWN、断线、skipped transition、启动等，§7）。"""
        return self._reconcile(triggered_by=reason, now_ms=int(self.clock()))

    # ------------------------------------------------------------------ Stop

    def stop(self, *, position_qty: float) -> StopReport:
        """§17：禁新增 → 撤 Probex 挂单 → drain → 确认 0 挂单；**绝不**自动市价平仓。"""
        from execution.types import OrderStatus as _Status

        self.state = OrchestratorState.STOPPED
        cancelled: list[str] = []
        for order in self._existing_orders():
            if order.status is _Status.PENDING_CANCEL:
                continue
            try:
                self.engine.cancel(order.client_order_id, now_ms=int(self.clock()))
                cancelled.append(order.client_order_id)
            except Exception:  # noqa: BLE001 - stop 必须继续撤其余订单
                continue
        for _ in range(3):
            self.engine.poll(now_ms=int(self.clock()))
        remaining = tuple(
            order.client_order_id
            for order in self.engine.manager.tracker.active()
            if order.symbol == self.config.symbol
        )
        notes: list[str] = []
        if remaining:
            notes.append("ACTIVE_ORDERS_REMAIN")
        if position_qty != 0.0:
            notes.append("POSITION_REMAINS")
        return StopReport(
            cancelled=tuple(cancelled),
            remaining_active=remaining,
            position_qty=position_qty,
            position_remains=position_qty != 0.0,
            notes=tuple(notes),
        )

    # ------------------------------------------------------------------ telemetry

    def _record(
        self,
        *,
        loop_ts: Milliseconds,
        state: MarketState | None,
        prediction: object | None,
        snapshot: object,
        decision: MakerDecision | None,
        notes: list[str],
        reconciliation: ReconciliationOutcome | None,
        submitted: int = 0,
        cancelled: int = 0,
        replaced: int = 0,
        risk_rejects: int = 0,
        unknown_submits: int = 0,
        unknown_cancels: int = 0,
    ) -> LoopTelemetry:
        tracker = self.engine.manager.tracker
        authority = self.authority_provider().authority
        verdict = self._authority_verdict() if authority is not None else None
        # prediction 身份用既有的 `request_id`（PredictionRecord 的 identity）；age 用响应到达时刻
        prediction_identity = None
        prediction_age = None
        if prediction is not None:
            prediction_identity = getattr(prediction, "prediction_id", None) or getattr(
                prediction, "request_id", None
            )
            received = getattr(prediction, "response_received_at", None) or getattr(prediction, "as_of", None)
            if isinstance(received, int):
                prediction_age = max(0, loop_ts - received)
        telemetry = LoopTelemetry(
            loop_ts=loop_ts,
            market_state_id=None if state is None else getattr(state.identity, "symbol", None),
            market_state_hash=None if state is None else getattr(state, "state_hash", None),
            market_healthy=bool(getattr(getattr(state, "quality", None), "tradeable", False)) if state else False,
            prediction_id=prediction_identity,
            prediction_age_ms=prediction_age,
            position_qty=float(getattr(snapshot, "position_qty", 0.0) or 0.0),
            open_order_exposure=tracker.total_pending_exposure(),
            uncertain_exposure=tracker.uncertain_exposure(),
            authority_id=None if authority is None else getattr(authority, "authority_id", None),
            authority_valid=bool(verdict.valid) if verdict is not None else False,
            authority_reason=None if verdict is None or verdict.valid else verdict.reasons[0].value,
            maker_decision="none" if decision is None else "decided",
            bid_action="none" if decision is None else decision.bid.action.value,
            ask_action="none" if decision is None else decision.ask.action.value,
            blocked_by=None if decision is None or decision.blocked_by is None else decision.blocked_by.value,
            submit_count=submitted,
            cancel_count=cancelled,
            replace_count=replaced,
            risk_reject_count=risk_rejects,
            unknown_submit_count=unknown_submits,
            unknown_cancel_count=unknown_cancels,
            skipped_transition_count=self.adapter.skipped_transition_count,
            reconciliation_triggered=reconciliation is not None,
            reconciliation_converged=None if reconciliation is None else reconciliation.converged,
            execution_disabled=not self.config.execution_enabled,
            notes=tuple(notes),
        )
        self.telemetry.record(telemetry)
        return telemetry


@dataclass(frozen=True, slots=True)
class _InvalidVerdict:
    """内部：authority 不可用时的等价判定（避免调用方分支）。

    `reason` 允许传裸情态：构造时统一归一为 1 元组（调用方仍按 `reasons[0]` 读取）。
    """

    reason: AuthorityInvalidReason | tuple[AuthorityInvalidReason, ...]
    detail: str

    @property
    def reasons(self) -> tuple[AuthorityInvalidReason, ...]:
        if isinstance(self.reason, tuple):
            return self.reason
        return (self.reason,)

    @property
    def valid(self) -> bool:
        return False


__all__ = [
    "ExecutionDisabledError",
    "LiveExecutionOrchestrator",
    "LoopOutcome",
    "LoopPhase",
    "OrderMultiplicityViolation",
    "OrchestratorConfig",
    "OrchestratorError",
    "OrchestratorState",
    "ReconciliationOutcome",
    "StopReport",
]
