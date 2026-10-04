"""ExecutionEngine：`OrderProposal` →（新鲜风险检查）→ submit → 事件回流记账。

顺序纪律（P0001.6 §14 / §15）：

```text
OrderProposal
  → 新鲜 RiskSnapshot（含 manager 的 open_order_exposure）
  → RiskGate
      ├── REJECT → ExecutionRejected（**不创建外部订单**，只产出 telemetry）
      └── ALLOW  → OrderManager.submit → ExecutionAdapter
外部事件 → OrderTracker → canonical Fill → AccountingCore
```

Kill switch 语义（§16）：

- `HALT_ALL`：禁止 submit（RiskGate 一律拒绝），**但仍允许 cancel 已存在订单**（本类不对 cancel 做门控）；
- `REDUCE_ONLY`：只有真正降低 exposure 的 proposal 能通过。

本类不 import Prediction / Jev / Strategy（SC-16）。
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from execution.events import ExecutionEvent
from execution.manager import OrderManager
from execution.normalization import (BoundedNormalizationEvidenceLog, OrderNormalizationError,
                                     OrderNormalizer, normalize_with_evidence)
from execution.types import OrderStatus
from execution.tracker import TrackerUpdate
from execution.types import Order
from market.events.types import Milliseconds
from portfolio.accounting import AccountingCore
from portfolio.types import Fill, LiquidationInfo
from risk.gate import RiskGate
from risk.snapshot import build_risk_snapshot, utc_day_start_ms
from risk.types import OrderProposal, RiskDecision, RiskSnapshot


@dataclass(frozen=True, slots=True)
class ExecutionRejected:
    """因风险被拒绝的提案（telemetry 用，不产生任何外部订单）。"""

    proposal: OrderProposal
    decision: RiskDecision
    timestamp: Milliseconds

    @property
    def reason_code(self) -> object:
        return self.decision.reason_code


@dataclass(frozen=True, slots=True)
class RiskDecisionObservation:
    """一次真实风险判定的只读观测（allow 与 reject 都记录；引用既有 RiskDecision，不复制）。"""

    decision: RiskDecision
    proposal: OrderProposal
    timestamp: Milliseconds
    client_order_id: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """一次引擎操作的结果。"""

    timestamp: Milliseconds
    snapshot: RiskSnapshot | None = None
    decision: RiskDecision | None = None
    order: Order | None = None
    updates: tuple[TrackerUpdate, ...] = ()
    rejection: ExecutionRejected | None = None
    fills: tuple[Fill, ...] = ()

    @property
    def submitted(self) -> bool:
        return self.order is not None

    @property
    def rejected(self) -> bool:
        return self.rejection is not None


@dataclass
class ExecutionEngine:
    """风险门控 + 订单生命周期 + 成交记账的组合入口。"""

    accounting: AccountingCore
    gate: RiskGate
    manager: OrderManager
    liquidation_provider: Callable[[str], LiquidationInfo | None] | None = None
    day_start_fn: Callable[[Milliseconds], Milliseconds] = utc_day_start_ms
    book_healthy: bool = True
    #: 旁路延迟观测（closure Slice 3 / F-05）：默认 None ⇒ 行为完全不变
    latency_observer: Callable[[str, int], None] | None = None
    #: F-08：有界风险判定观测（allow + reject 都进 trace；默认仅保留最近 200 条）
    decision_capacity: int = 200
    #: F-08：执行边界的显式归一化（None ⇒ 不归一，行为与既有完全一致）
    normalizer: OrderNormalizer | None = None
    normalization_capacity: int = 200
    rejections: list[ExecutionRejected] = field(default_factory=list)
    _decisions: deque = field(default_factory=deque, init=False)
    _normalization: BoundedNormalizationEvidenceLog = field(init=False)

    def __post_init__(self) -> None:
        if isinstance(self.decision_capacity, bool) or not isinstance(self.decision_capacity, int) \
                or self.decision_capacity <= 0:
            raise ValueError("decision_capacity must be a positive int")
        self._decisions = deque(self._decisions, maxlen=self.decision_capacity)
        self._normalization = BoundedNormalizationEvidenceLog(capacity=self.normalization_capacity)

    @property
    def decision_log(self) -> tuple[RiskDecisionObservation, ...]:
        """风险判定观测（allow 与 reject）；只读，供产品层组成 trace。"""
        return tuple(self._decisions)

    @property
    def normalization_log(self) -> BoundedNormalizationEvidenceLog:
        """执行边界归一化证据（未配置 normalizer 时为空 ⇒ 产品层如实 ABSENT）。"""
        return self._normalization

    # ------------------------------------------------------------------ submit

    def _note_latency(self, kind: str, ts_ms: int) -> None:
        """真实边界观测（失败绝不影响交易路径）。"""
        observer = self.latency_observer
        if observer is None:
            return
        try:
            observer(kind, int(ts_ms))
        except Exception:  # noqa: BLE001
            return

    def submit(self, proposal: OrderProposal, *, now_ms: Milliseconds) -> ExecutionResult:
        """在**紧邻 submit** 的位置做一次新鲜风险检查。"""
        snapshot = self.snapshot(proposal.symbol, now_ms=now_ms)
        decision = self.gate.evaluate(proposal, snapshot, book_healthy=self.book_healthy)
        if decision.rejected:
            rejection = ExecutionRejected(proposal=proposal, decision=decision, timestamp=now_ms)
            self.rejections.append(rejection)
            self._decisions.append(RiskDecisionObservation(decision=decision, proposal=proposal,
                                                           timestamp=now_ms, client_order_id=None))
            return ExecutionResult(timestamp=now_ms, snapshot=snapshot, decision=decision, rejection=rejection)

        # F-08：执行边界归一化（只有显式配置时才发生；evidence 是只读事实）
        if self.normalizer is not None:
            normalized, evidence = normalize_with_evidence(
                self.normalizer, price=proposal.price, quantity=proposal.quantity, ts=now_ms,
                client_order_id=None, log=self._normalization)
            if normalized is None:
                raise OrderNormalizationError(evidence.reject_reason or "order normalization rejected")
            proposal = replace(proposal, price=float(normalized.price),
                               quantity=float(normalized.quantity))

        self._note_latency("submit_request", now_ms)
        order, updates = self.manager.submit(proposal, timestamp=now_ms)
        # 归一化 evidence 在"建单前"产生，这里把 canonical identity 补上（顺序不可颠倒）
        self._normalization.bind_client_order_id(order.client_order_id)
        self._decisions.append(RiskDecisionObservation(decision=decision, proposal=proposal,
                                                       timestamp=now_ms,
                                                       client_order_id=order.client_order_id))
        if order.status in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED):
            self._note_latency("event:OrderAccepted", int(order.updated_at))
        return ExecutionResult(
            timestamp=now_ms,
            snapshot=snapshot,
            decision=decision,
            order=order,
            updates=updates,
            fills=self._account(updates),
        )

    def orders_for_decision(self, decision_id: str) -> tuple[Order, ...]:
        """`Decision → Order(s)`：按订单自身的 canonical correlation 反查（P0001.15 §15 / SC-27）。"""
        return self.manager.orders_for_decision(decision_id)

    def cancel(self, client_order_id: str, *, now_ms: Milliseconds) -> ExecutionResult:
        """撤单**不经过 RiskGate**：kill switch 的 HALT_ALL 不能把已有挂单锁在市场上。"""
        self._note_latency("cancel_request", now_ms)
        updates = self.manager.cancel(client_order_id, timestamp=now_ms)
        after = self.manager.tracker.order(client_order_id)
        if after is not None and after.status is OrderStatus.CANCELED:
            self._note_latency("update:Canceled", int(after.updated_at))
        return ExecutionResult(timestamp=now_ms, updates=updates, fills=self._account(updates))

    def on_events(self, events: tuple[ExecutionEvent, ...], *, now_ms: Milliseconds) -> ExecutionResult:
        for event in events:
            self._note_latency(f"event:{type(event).__name__}", int(getattr(event, "timestamp", now_ms)))
        updates = self.manager.on_events(events)
        return ExecutionResult(timestamp=now_ms, updates=updates, fills=self._account(updates))

    def poll(self, *, now_ms: Milliseconds) -> ExecutionResult:
        updates = self.manager.poll()
        for update in updates:
            self._note_latency(f"update:{type(update).__name__}", int(now_ms))
        return ExecutionResult(timestamp=now_ms, updates=updates, fills=self._account(updates))

    # ------------------------------------------------------------------ 快照

    def snapshot(self, symbol: str, *, now_ms: Milliseconds) -> RiskSnapshot:
        """构建新鲜快照：pending exposure = confirmed(ACTIVE) + uncertain(LOST)（§15 / P0001.6.1）。

        mark price 由调用方通过 `AccountingCore.update_mark_price` 注入（本类不隐式改账户状态）。
        """
        liquidation = self.liquidation_provider(symbol) if self.liquidation_provider is not None else None
        return build_risk_snapshot(
            self.accounting,
            symbol=symbol,
            now_ms=now_ms,
            open_order_exposure=self.manager.open_order_exposure(),
            confirmed_open_exposure=self.manager.confirmed_open_exposure,
            uncertain_exposure=self.manager.uncertain_exposure,
            unresolved_order_count=self.manager.unresolved_order_count,
            liquidation=liquidation,
            day_start_ts=self.day_start_fn(now_ms),
        )

    # ------------------------------------------------------------------ 内部

    def _account(self, updates: tuple[TrackerUpdate, ...]) -> tuple[Fill, ...]:
        """canonical Fill → AccountingCore（含 accounting 侧第二道去重）。"""
        applied: list[Fill] = []
        for update in updates:
            if update.fill is None:
                continue
            application = self.accounting.record_fill(update.fill)
            if application.accepted:
                applied.append(update.fill)
        return tuple(applied)


__all__ = ["ExecutionEngine", "ExecutionRejected", "ExecutionResult"]
