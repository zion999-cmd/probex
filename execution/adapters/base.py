"""ExecutionAdapter 契约（P0001.6）。

所有 adapter 统一输出 `ExecutionEvent`；tracker 是本地状态权威，adapter 只是外部事实来源。

第一版接口是**同步**的：PaperBroker 与测试都在同一调用栈内完成。真实 Binance adapter（P0001.9）
需要网络与 user stream，将通过新的契约决策（异步 / 事件队列桥接）引入，不改动 tracker 的语义
（见 `context/decisions.md` D-021）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from execution.events import ExecutionEvent
from execution.types import ExternalFill, ExternalOrder, Order
from market.events.types import Milliseconds


@runtime_checkable
class ExecutionAdapter(Protocol):
    """下单 / 撤单 / 拉取外部事实。"""

    def submit(self, order: Order) -> tuple[ExecutionEvent, ...]:
        """提交订单，返回 ack/reject 事件（可能为空：提交结果未知）。"""

    def cancel(self, order: Order) -> tuple[ExecutionEvent, ...]:
        """发送撤单请求，返回撤单**确认**事件（为空表示尚未确认）。"""

    def poll(self) -> tuple[ExecutionEvent, ...]:
        """拉取自上次 poll 以来的外部事件（fills / 状态变化）。"""

    def open_orders(self) -> tuple[ExternalOrder, ...]:
        """外部当前挂单（reconciliation 输入）。"""

    def recent_fills(self, *, since_ms: Milliseconds | None = None) -> tuple[ExternalFill, ...]:
        """外部近期成交（reconciliation 输入）。"""


__all__ = ["ExecutionAdapter"]
