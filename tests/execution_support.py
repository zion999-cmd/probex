"""执行层测试脚手架：把 accounting / risk / paper broker 组装成一个可控栈。

用于 unit / integration / fault / replay 各层测试，避免每个测试重复搭线。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from execution import ExecutionEngine, ExecutionResult, OrderManager, OrderTracker, PaperBroker
from execution.reconciliation import ReconciliationReport, reconcile
from execution.types import Order
from market.events.types import Milliseconds
from portfolio.accounting import AccountingCore
from portfolio.types import LiquidationInfo, Side
from risk.gate import RiskGate
from risk.limits import RiskLimits
from risk.types import OrderProposal
from tests.support import BASE_TS, SYMBOL


@dataclass
class ExecutionStack:
    """一个完整的 Paper 执行栈。"""

    accounting: AccountingCore
    broker: PaperBroker
    tracker: OrderTracker
    manager: OrderManager
    gate: RiskGate
    engine: ExecutionEngine
    symbol: str = SYMBOL

    @classmethod
    def build(
        cls,
        *,
        balance: float = 10_000.0,
        mark: float | None = 100.0,
        limits: RiskLimits | None = None,
        session: str = "s1",
        book_healthy: bool = True,
        liquidation_provider: Callable[[str], LiquidationInfo | None] | None = None,
        symbol: str = SYMBOL,
    ) -> ExecutionStack:
        accounting = AccountingCore(initial_balance=balance)
        if mark is not None:
            accounting.update_mark_price(symbol, mark, timestamp=BASE_TS)
        broker = PaperBroker()
        tracker = OrderTracker(session_id=session)
        manager = OrderManager(tracker=tracker, adapter=broker)
        gate = RiskGate(limits if limits is not None else RiskLimits(max_position_qty=10.0))
        engine = ExecutionEngine(
            accounting=accounting,
            gate=gate,
            manager=manager,
            book_healthy=book_healthy,
            liquidation_provider=liquidation_provider,
        )
        return cls(
            accounting=accounting,
            broker=broker,
            tracker=tracker,
            manager=manager,
            gate=gate,
            engine=engine,
            symbol=symbol,
        )

    # ------------------------------------------------------------------ 便捷操作

    def proposal(
        self,
        *,
        side: Side = Side.BUY,
        quantity: float = 1.0,
        price: float = 100.0,
        reduce_only: bool = False,
        post_only: bool = False,
    ) -> OrderProposal:
        return OrderProposal(
            symbol=self.symbol,
            side=side,
            quantity=quantity,
            price=price,
            reduce_only=reduce_only,
            post_only=post_only,
        )

    def submit(self, proposal: OrderProposal | None = None, *, now_ms: Milliseconds = BASE_TS) -> ExecutionResult:
        return self.engine.submit(proposal or self.proposal(), now_ms=now_ms)

    def submit_order(self, proposal: OrderProposal | None = None, *, now_ms: Milliseconds = BASE_TS) -> Order:
        result = self.submit(proposal, now_ms=now_ms)
        assert result.order is not None, f"submit was rejected: {result.rejection}"
        return result.order

    def fill(
        self,
        order: Order | str,
        *,
        quantity: float = 1.0,
        price: float | None = None,
        fee: float = 0.0,
        timestamp: Milliseconds = BASE_TS + 10,
        execution_id: str | None = None,
        trade_id: str | None = None,
        poll: bool = True,
        update_external: bool = True,
    ) -> ExecutionResult:
        client_order_id = order if isinstance(order, str) else order.client_order_id
        resolved_price = price if price is not None else self.order(client_order_id).price
        self.broker.fill(
            client_order_id,
            quantity=quantity,
            price=resolved_price,
            fee=fee,
            timestamp=timestamp,
            execution_id=execution_id,
            trade_id=trade_id,
            update_external=update_external,
        )
        if not poll:
            return ExecutionResult(timestamp=timestamp)
        return self.poll(now_ms=timestamp)

    def poll(self, *, now_ms: Milliseconds = BASE_TS + 10) -> ExecutionResult:
        return self.engine.poll(now_ms=now_ms)

    def cancel(self, order: Order | str, *, now_ms: Milliseconds = BASE_TS + 20) -> ExecutionResult:
        client_order_id = order if isinstance(order, str) else order.client_order_id
        return self.engine.cancel(client_order_id, now_ms=now_ms)

    def order(self, client_order_id: str) -> Order:
        return self.tracker.require_order(client_order_id)

    def reconcile(self, *, timestamp: Milliseconds = BASE_TS + 100, fills: bool = True) -> ReconciliationReport:
        report = reconcile(
            self.tracker,
            external_open_orders=self.broker.open_orders(),
            external_recent_fills=self.broker.recent_fills() if fills else (),
            timestamp=timestamp,
        )
        for fill in report.canonical_fills:
            self.accounting.record_fill(fill)
        return report

    # ------------------------------------------------------------------ 状态快照（确定性比较用）

    def state(self) -> tuple:
        position = self.accounting.position(self.symbol)
        return (
            self.tracker.orders,
            self.accounting.fills.fills,
            (position.qty, position.avg_entry_price, position.realized_pnl),
            self.accounting.balance,
            self.accounting.equity(),
            self.manager.open_order_exposure(),
        )


@dataclass
class OrderStackBuilder:
    """保留给需要逐步构造栈的测试（例如先建仓再挂单）。"""

    stacks: list[ExecutionStack] = field(default_factory=list)


__all__ = ["ExecutionStack", "OrderStackBuilder"]
