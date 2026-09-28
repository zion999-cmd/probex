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

from collections.abc import Callable
from dataclasses import dataclass, field

from execution.events import ExecutionEvent
from execution.manager import OrderManager
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
    rejections: list[ExecutionRejected] = field(default_factory=list)

    # ------------------------------------------------------------------ submit

    def submit(self, proposal: OrderProposal, *, now_ms: Milliseconds) -> ExecutionResult:
        """在**紧邻 submit** 的位置做一次新鲜风险检查。"""
        snapshot = self.snapshot(proposal.symbol, now_ms=now_ms)
        decision = self.gate.evaluate(proposal, snapshot, book_healthy=self.book_healthy)
        if decision.rejected:
            rejection = ExecutionRejected(proposal=proposal, decision=decision, timestamp=now_ms)
            self.rejections.append(rejection)
            return ExecutionResult(timestamp=now_ms, snapshot=snapshot, decision=decision, rejection=rejection)

        order, updates = self.manager.submit(proposal, timestamp=now_ms)
        return ExecutionResult(
            timestamp=now_ms,
            snapshot=snapshot,
            decision=decision,
            order=order,
            updates=updates,
            fills=self._account(updates),
        )

    def cancel(self, client_order_id: str, *, now_ms: Milliseconds) -> ExecutionResult:
        """撤单**不经过 RiskGate**：kill switch 的 HALT_ALL 不能把已有挂单锁在市场上。"""
        updates = self.manager.cancel(client_order_id, timestamp=now_ms)
        return ExecutionResult(timestamp=now_ms, updates=updates, fills=self._account(updates))

    def on_events(self, events: tuple[ExecutionEvent, ...], *, now_ms: Milliseconds) -> ExecutionResult:
        updates = self.manager.on_events(events)
        return ExecutionResult(timestamp=now_ms, updates=updates, fills=self._account(updates))

    def poll(self, *, now_ms: Milliseconds) -> ExecutionResult:
        updates = self.manager.poll()
        return ExecutionResult(timestamp=now_ms, updates=updates, fills=self._account(updates))

    # ------------------------------------------------------------------ 快照

    def snapshot(self, symbol: str, *, now_ms: Milliseconds) -> RiskSnapshot:
        """构建新鲜快照：pending exposure 来自 manager 的 active 订单（§15）。

        mark price 由调用方通过 `AccountingCore.update_mark_price` 注入（本类不隐式改账户状态）。
        """
        liquidation = self.liquidation_provider(symbol) if self.liquidation_provider is not None else None
        return build_risk_snapshot(
            self.accounting,
            symbol=symbol,
            now_ms=now_ms,
            open_order_exposure=self.manager.open_order_exposure(),
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
