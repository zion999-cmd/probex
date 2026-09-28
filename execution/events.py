"""ExecutionEvent：所有 ExecutionAdapter 的统一输出（P0001.6 §3）。

ExecutionEngine 不直接修改 Accounting：

```text
ExecutionEvent → OrderTracker → canonical Fill → AccountingCore
```
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias

from execution.types import OrderStatus
from market.events.types import Milliseconds


class ExecutionEventType(Enum):
    ORDER_ACCEPTED = "order_accepted"
    ORDER_REJECTED = "order_rejected"
    ORDER_CANCELED = "order_canceled"
    ORDER_EXPIRED = "order_expired"
    FILL_RECEIVED = "fill_received"
    ORDER_STATUS_UPDATE = "order_status_update"


@dataclass(frozen=True, slots=True)
class OrderAccepted:
    client_order_id: str
    exchange_order_id: str
    timestamp: Milliseconds
    event_type: ExecutionEventType = ExecutionEventType.ORDER_ACCEPTED


@dataclass(frozen=True, slots=True)
class OrderRejected:
    client_order_id: str
    reason: str
    timestamp: Milliseconds
    exchange_order_id: str | None = None
    event_type: ExecutionEventType = ExecutionEventType.ORDER_REJECTED


@dataclass(frozen=True, slots=True)
class OrderCanceled:
    """撤单**已确认**（不是「已发送撤单」）。"""

    client_order_id: str
    timestamp: Milliseconds
    executed_quantity: float | None = None
    event_type: ExecutionEventType = ExecutionEventType.ORDER_CANCELED


@dataclass(frozen=True, slots=True)
class OrderExpired:
    client_order_id: str
    timestamp: Milliseconds
    event_type: ExecutionEventType = ExecutionEventType.ORDER_EXPIRED


@dataclass(frozen=True, slots=True)
class FillReceived:
    """一笔成交（execution 层去重键：`execution_id` 与 `trade_id`）。"""

    client_order_id: str
    execution_id: str
    trade_id: str
    price: float
    quantity: float
    timestamp: Milliseconds
    fee: float = 0.0
    fee_asset: str = "USDT"
    is_maker: bool | None = None
    event_type: ExecutionEventType = ExecutionEventType.FILL_RECEIVED


@dataclass(frozen=True, slots=True)
class OrderStatusUpdate:
    """外部主动上报的状态变化（含 LOST 与 reconciliation 结论）。"""

    client_order_id: str
    status: OrderStatus
    timestamp: Milliseconds
    detail: str = ""
    exchange_order_id: str | None = None
    filled_quantity: float | None = None
    avg_fill_price: float | None = None
    event_type: ExecutionEventType = ExecutionEventType.ORDER_STATUS_UPDATE


ExecutionEvent: TypeAlias = (
    OrderAccepted | OrderRejected | OrderCanceled | OrderExpired | FillReceived | OrderStatusUpdate
)

EVENT_TYPES: tuple[type, ...] = (
    OrderAccepted,
    OrderRejected,
    OrderCanceled,
    OrderExpired,
    FillReceived,
    OrderStatusUpdate,
)

__all__ = [
    "EVENT_TYPES",
    "ExecutionEvent",
    "ExecutionEventType",
    "FillReceived",
    "OrderAccepted",
    "OrderCanceled",
    "OrderExpired",
    "OrderRejected",
    "OrderStatusUpdate",
]
