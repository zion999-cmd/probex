"""P0001.8 测试脚手架：把 `SimulatedVenue` 与既有执行栈组装成可控回放环境。

约定：

- 市场事件的时间轴用 `ts(i) = BASE_TS + i`（毫秒）显式推进，不使用 wall-clock；
- `feed()` 逐事件驱动：`venue.on_market_event(event)` → `venue.poll()` → `engine.on_events(...)`，
  与真实回放循环一致（也便于断言新产生的外部事件）。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from execution import ExecutionEngine, ExecutionResult, OrderManager, OrderTracker
from execution.events import ExecutionEvent
from execution.simulation import FeeSchedule, LatencyModel, SimulatedVenue
from execution.types import Order
from market.events.payloads import AggressorSide
from market.events.types import EventType, MarketEvent
from portfolio.accounting import AccountingCore
from portfolio.types import LiquidationInfo, Side
from risk.gate import RiskGate
from risk.limits import RiskLimits
from risk.types import OrderProposal
from tests.support import BASE_TS, SYMBOL, depth_diff_event, depth_snapshot_event, trade_event

#: 测试费率（生产费率必须由人类给定）。
DEFAULT_TEST_FEE_RATE = 0.0002


def ts(offset: int) -> int:
    """标准时间轴：`BASE_TS + offset`（毫秒）。"""
    return BASE_TS + offset


def book_snapshot(
    *,
    update_id: int = 1,
    bids: tuple[tuple[float, float], ...] = ((100.0, 3.0),),
    asks: tuple[tuple[float, float], ...] = ((101.0, 2.0),),
    offset: int = 0,
    symbol: str = SYMBOL,
) -> MarketEvent:
    """深度快照（同时把盘口推进到 HEALTHY）。"""
    return depth_snapshot_event(
        update_id,
        symbol=symbol,
        bids=bids,
        asks=asks,
        exchange_ts=ts(offset),
        receive_ts=ts(offset),
        process_ts=ts(offset),
    )


def book_delta(
    first_update_id: int,
    last_update_id: int,
    *,
    bids: tuple[tuple[float, float], ...] = (),
    asks: tuple[tuple[float, float], ...] = (),
    offset: int = 0,
    symbol: str = SYMBOL,
) -> MarketEvent:
    """深度增量（`size == 0` 表示删除该档位）。"""
    return depth_diff_event(
        first_update_id,
        last_update_id,
        symbol=symbol,
        bids=bids,
        asks=asks,
        exchange_ts=ts(offset),
        receive_ts=ts(offset),
        process_ts=ts(offset),
    )


def trade(
    aggregate_trade_id: int,
    *,
    price: float,
    quantity: float,
    aggressor: AggressorSide,
    offset: int = 0,
    symbol: str = SYMBOL,
) -> MarketEvent:
    """aggressor 成交事件（唯一的成交证据）。"""
    return trade_event(
        aggregate_trade_id,
        price=price,
        quantity=quantity,
        aggressor=aggressor,
        symbol=symbol,
        exchange_ts=ts(offset),
        receive_ts=ts(offset),
        process_ts=ts(offset),
    )


def sell_aggressor(aggregate_trade_id: int, *, price: float, quantity: float, offset: int = 0) -> MarketEvent:
    return trade(aggregate_trade_id, price=price, quantity=quantity, aggressor=AggressorSide.SELL, offset=offset)


def buy_aggressor(aggregate_trade_id: int, *, price: float, quantity: float, offset: int = 0) -> MarketEvent:
    return trade(aggregate_trade_id, price=price, quantity=quantity, aggressor=AggressorSide.BUY, offset=offset)


@dataclass
class SimStack:
    """`SimulatedVenue` 执行栈。"""

    accounting: AccountingCore
    venue: SimulatedVenue
    tracker: OrderTracker
    manager: OrderManager
    gate: RiskGate
    engine: ExecutionEngine
    symbol: str = SYMBOL
    produced: list[ExecutionEvent] = field(default_factory=list)
    last_event_ts: int = BASE_TS

    @classmethod
    def build(
        cls,
        *,
        balance: float = 10_000.0,
        mark: float | None = 100.0,
        limits: RiskLimits | None = None,
        submit_latency_ms: int = 0,
        cancel_latency_ms: int = 0,
        maker_fee_rate: float = DEFAULT_TEST_FEE_RATE,
        fee_asset: str = "USDT",
        session: str = "s1",
        symbol: str = SYMBOL,
        liquidation_provider: object = None,
    ) -> SimStack:
        accounting = AccountingCore(initial_balance=balance)
        if mark is not None:
            accounting.update_mark_price(symbol, mark, timestamp=BASE_TS)
        venue = SimulatedVenue(
            symbol=symbol,
            latency=LatencyModel(submit_latency_ms=submit_latency_ms, cancel_latency_ms=cancel_latency_ms),
            fee_schedule=FeeSchedule(maker_fee_rate=maker_fee_rate, fee_asset=fee_asset),
        )
        tracker = OrderTracker(session_id=session)
        manager = OrderManager(tracker=tracker, adapter=venue)
        gate = RiskGate(limits if limits is not None else RiskLimits(max_position_qty=10.0))
        engine = ExecutionEngine(
            accounting=accounting,
            gate=gate,
            manager=manager,
            book_healthy=True,
            liquidation_provider=liquidation_provider,  # type: ignore[arg-type]
        )
        return cls(
            accounting=accounting,
            venue=venue,
            tracker=tracker,
            manager=manager,
            gate=gate,
            engine=engine,
            symbol=symbol,
        )

    # ------------------------------------------------------------------ 操作

    def proposal(
        self,
        *,
        side: Side = Side.BUY,
        quantity: float = 1.0,
        price: float = 100.0,
        reduce_only: bool = False,
        post_only: bool = True,
    ) -> OrderProposal:
        return OrderProposal(
            symbol=self.symbol,
            side=side,
            quantity=quantity,
            price=price,
            reduce_only=reduce_only,
            post_only=post_only,
        )

    def submit(self, proposal: OrderProposal | None = None, *, now_ms: int = BASE_TS) -> ExecutionResult:
        return self.engine.submit(proposal or self.proposal(), now_ms=now_ms)

    def submit_order(self, proposal: OrderProposal | None = None, *, now_ms: int = BASE_TS) -> Order:
        result = self.submit(proposal, now_ms=now_ms)
        assert result.order is not None, f"submit was rejected: {result.rejection}"
        return result.order

    def cancel(self, order: Order | str, *, now_ms: int) -> ExecutionResult:
        client_order_id = order if isinstance(order, str) else order.client_order_id
        return self.engine.cancel(client_order_id, now_ms=now_ms)

    def feed(self, events: Iterable[MarketEvent]) -> tuple[ExecutionEvent, ...]:
        """逐事件驱动：喂给 venue，再把新产生的外部事件交给 engine。"""
        produced: list[ExecutionEvent] = []
        for event in events:
            self.last_event_ts = event.process_ts
            self.venue.on_market_event(event)
            events_out = self.venue.poll()
            self.produced.extend(events_out)
            produced.extend(events_out)
            self.engine.on_events(events_out, now_ms=event.process_ts)
        return tuple(produced)

    def order(self, client_order_id: str) -> Order:
        return self.tracker.require_order(client_order_id)

    def reconcile(self):
        from execution.reconciliation import reconcile

        report = reconcile(
            self.tracker,
            external_open_orders=self.venue.open_orders(),
            external_recent_fills=self.venue.recent_fills(),
            timestamp=self.last_event_ts,
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
            (self.accounting.balance, self.accounting.equity(), self.accounting.trading_fees),
            self.venue.order_views(),
            self.venue.fills,
        )


__all__ = [
    "DEFAULT_TEST_FEE_RATE",
    "EventType",
    "SimStack",
    "book_delta",
    "book_snapshot",
    "buy_aggressor",
    "sell_aggressor",
    "trade",
    "ts",
]
