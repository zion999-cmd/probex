"""公开行情源适配器协议（统一数据契约的扩展点）。

目的（补充任务：行情数据能力查漏补缺）
------------------------------------
让**任何**免费公开行情或未来付费行情源都能通过实现本适配器接入，而不修改核心账本、风险或
订单状态所有权。所有数据源必须转换为**既有内部契约 `MarketState`**——不为每个数据源另建状态机
或存储。

适配器责任
----------
1. 建立/维护到数据源的连接（可含其自身重连策略，但失败必须如实暴露，不得伪报成功）；
2. 每次 `poll_states()` 返回自上次调用以来产生的 **0..N 个 `MarketState`**（只读，按时间序）；
3. 可选 `latest_mark()` 提供正式参考价（无则 None；不允许用 last trade 伪造 mark）。

这是 `venue.contracts.MarketDataConnector` 之上、**面向 pump 的窄契约**：connector 描述一个
公开行情表面（facts/rules/health），本适配器只负责"产出 MarketState"。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from market.state.types import MarketState
from market.events.types import Milliseconds


@runtime_checkable
class MarketSourceAdapter(Protocol):
    """一个公开行情源到 `MarketState` 的适配器（只读）。"""

    @property
    def source_id(self) -> str:
        """稳定来源标识（写入事实，便于追溯；如 'binance-public'）。"""
        ...

    def connect(self) -> None:
        """建立行情源连接（失败抛出；不吞错）。"""
        ...

    def disconnect(self) -> None:
        """断开并释放连接。"""
        ...

    def poll_states(self, *, timeout_s: float, max_states: int) -> tuple[MarketState, ...]:
        """返回自上次调用以来新增的 MarketState（无新数据 ⇒ 空元组；失败如实抛出）。"""
        ...

    def latest_mark(self) -> object | None:
        """最新正式参考价观测（exchange_ts/price；无 ⇒ None）。"""
        ...


__all__ = ["MarketSourceAdapter"]
