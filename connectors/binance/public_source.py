"""Binance 公开行情 runtime → `MarketSourceAdapter`。

薄适配器：复用既有 `LiveMarketDataRuntime`（P0001.9.1 已验证的公开 REST/WS；无需凭据）。
只做"调用 pump_once 并取出 MarketState"，不重写任何传输或归一化逻辑。
"""

from __future__ import annotations

from collections.abc import Callable

from market.state.types import MarketState


class BinancePublicSourceAdapter:
    """`LiveMarketDataRuntime` → 统一 `MarketSourceAdapter`。"""

    def __init__(
        self,
        runtime: object,
        *,
        source_id: str = "binance-public",
    ) -> None:
        self._runtime = runtime
        self._source_id = source_id

    @property
    def source_id(self) -> str:
        return self._source_id

    def connect(self) -> None:
        self._runtime.connect()

    def disconnect(self) -> None:
        self._runtime.close()

    def poll_states(self, *, timeout_s: float, max_states: int) -> tuple[MarketState, ...]:
        batch = self._runtime.pump_once(timeout_s=timeout_s, max_messages=max_states)
        return tuple(batch.states)

    def latest_mark(self) -> object | None:
        return self._runtime.latest_mark


__all__ = ["BinancePublicSourceAdapter"]
