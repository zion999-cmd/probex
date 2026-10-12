"""P0017.4：多标的模拟执行适配器（fan-out/fan-in）。

一个 `ExecutionAdapter` 门面，按 `order.symbol` / 事件 symbol 路由到各自的
`SimulatedVenue`（每个标的独立执行状态、互不污染），并把 poll 结果合并为统一事件流。

- 不复制撮合逻辑（全部委托给既有 SimulatedVenue）。
- 同一 symbol 的 submit/cancel/poll 顺序由调用方（driver 单线程）保证。
- 未知 symbol 显式报错（不静默吞单）。
"""

from __future__ import annotations

from market.events.types import MarketEvent, Milliseconds
from execution.adapters.base import ExecutionAdapter
from execution.events import ExecutionEvent
from execution.simulation.venue import SimulatedVenue
from execution.types import ExternalFill, ExternalOrder


class MultiSymbolSimulatedAdapter:
    """多 SimulatedVenue 的组合，实现单标的 ExecutionAdapter 的同一接口形状。"""

    def __init__(self, venues: dict[str, SimulatedVenue]) -> None:
        if not venues:
            raise ValueError("MultiSymbolSimulatedAdapter requires at least one venue")
        self._venues: dict[str, SimulatedVenue] = dict(venues)

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self._venues))

    def venue(self, symbol: str) -> SimulatedVenue:
        try:
            return self._venues[symbol]
        except KeyError:
            raise KeyError(f"no simulated venue configured for symbol {symbol!r}") from None

    def on_market_event(self, event: MarketEvent) -> None:
        """市场事件只路由给对应 symbol 的 venue（其他 venue 不可见该标的状态）。"""
        self.venue(event.symbol).on_market_event(event)

    # ------------------------------------------------------------- ExecutionAdapter

    def submit(self, order) -> tuple[ExecutionEvent, ...]:
        return self.venue(order.symbol).submit(order)

    def cancel(self, order) -> tuple[ExecutionEvent, ...]:
        return self.venue(order.symbol).cancel(order)

    def poll(self) -> tuple[ExecutionEvent, ...]:
        """合并所有 venue 的 outbox；顺序：按 symbol 名字排序（确定性，跨标的平局规则）。"""
        events: list[ExecutionEvent] = []
        for symbol in self.symbols:
            events.extend(self._venues[symbol].poll())
        return tuple(events)

    def open_orders(self) -> tuple[ExternalOrder, ...]:
        orders: list[ExternalOrder] = []
        for symbol in self.symbols:
            orders.extend(self._venues[symbol].open_orders())
        return tuple(orders)

    def recent_fills(self, *, since_ms: Milliseconds | None = None) -> tuple[ExternalFill, ...]:
        fills: list[ExternalFill] = []
        for symbol in self.symbols:
            fills.extend(self._venues[symbol].recent_fills(since_ms=since_ms))
        return tuple(fills)


__all__ = ["MultiSymbolSimulatedAdapter"]
