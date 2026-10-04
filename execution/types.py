"""执行领域类型：Order、OrderStatus，以及外部事实类型。

`Order` 是不可变快照：OrderTracker 在每次状态转换时替换它，因此任何持有者看到的都是
某一个确定时刻的订单事实（与 `MarketState` / `RiskSnapshot` 的纪律一致）。

`OrderProposal`（P0001.5）只是意图；本模块的 `Order` 才有生命周期字段，二者严格分开。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from enum import Enum

from market.events.types import Milliseconds, Venue
from risk.types import OrderCorrelation
from portfolio.types import Side

#: 第一版只支持 LIMIT 订单（post_only 是 LIMIT 的属性）。
LIMIT_ORDER_TYPE = "limit"


class ExecutionError(Exception):
    """执行领域错误基类。"""


class InvalidOrderError(ExecutionError):
    """订单不满足不变量。"""


class OrderNotFoundError(ExecutionError):
    """本地不存在该订单。"""


class IllegalOrderTransition(ExecutionError):
    """非法的状态转换。"""


class OrderStatus(Enum):
    """本地订单状态（第一版固定九态）。"""

    PENDING_CREATE = "PENDING_CREATE"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    PENDING_CANCEL = "PENDING_CANCEL"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    #: 本地无法确认外部状态；**不是事实终态**，可被 reconciliation 恢复。
    LOST = "LOST"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATUSES

    @property
    def is_active(self) -> bool:
        """当前可能产生 exposure（用于 active 视图与 open-order exposure）。"""
        return self in _ACTIVE_STATUSES

    @property
    def is_lost(self) -> bool:
        return self is OrderStatus.LOST


_TERMINAL_STATUSES = frozenset(
    {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.FAILED, OrderStatus.EXPIRED}
)
_ACTIVE_STATUSES = frozenset(
    {
        OrderStatus.PENDING_CREATE,
        OrderStatus.OPEN,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.PENDING_CANCEL,
    }
)

#: 允许的状态转换（`LOST` 可与 active/终态互相转换，因为它是「不确定」而非终态）。
ALLOWED_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PENDING_CREATE: frozenset(
        {OrderStatus.OPEN, OrderStatus.FAILED, OrderStatus.LOST, OrderStatus.EXPIRED}
    ),
    OrderStatus.OPEN: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.PENDING_CANCEL,
            OrderStatus.CANCELED,
            OrderStatus.EXPIRED,
            OrderStatus.FAILED,
            OrderStatus.LOST,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.PENDING_CANCEL,
            OrderStatus.EXPIRED,
            OrderStatus.LOST,
        }
    ),
    OrderStatus.PENDING_CANCEL: frozenset(
        {
            OrderStatus.CANCELED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.PENDING_CANCEL,
            OrderStatus.LOST,
        }
    ),
    # 终态：不接受状态回退；late fill 只更新成交事实，不改状态
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELED: frozenset(),
    OrderStatus.FAILED: frozenset(),
    OrderStatus.EXPIRED: frozenset(),
    # LOST 可以被 reconciliation 恢复为任何真实状态
    OrderStatus.LOST: frozenset(
        {
            OrderStatus.PENDING_CREATE,
            OrderStatus.OPEN,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.PENDING_CANCEL,
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.FAILED,
            OrderStatus.EXPIRED,
        }
    ),
}


def can_transition(source: OrderStatus, target: OrderStatus) -> bool:
    """同态也必须在允许集合里显式列出（例如 PENDING_CANCEL → PENDING_CANCEL）。"""
    return target in ALLOWED_TRANSITIONS[source]


@dataclass(frozen=True, slots=True)
class Order:
    """本地订单事实（不可变快照）。"""

    client_order_id: str
    venue: Venue
    symbol: str
    side: Side
    price: float
    quantity: float
    status: OrderStatus
    created_at: Milliseconds
    updated_at: Milliseconds
    exchange_order_id: str | None = None
    order_type: str = LIMIT_ORDER_TYPE
    filled_quantity: float = 0.0
    avg_fill_price: float = 0.0
    reduce_only: bool = False
    post_only: bool = False
    #: 终态时刻的累计成交量；late fill 只改它，不改 status
    final_executed_quantity: float | None = None
    #: LOST 的原因（审计用）
    lost_reason: str | None = None
    #: canonical correlation metadata（P0001.15 §15 / SC-26）：由提案带入，随订单生命周期存在
    correlation: OrderCorrelation | None = None

    def __post_init__(self) -> None:
        for field in ("client_order_id", "symbol"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value:
                raise InvalidOrderError(f"Order.{field} must be a non-empty string")
        if not isinstance(self.venue, Venue):
            raise InvalidOrderError(f"Order.venue must be a Venue, got {type(self.venue).__name__}")
        if not isinstance(self.side, Side):
            raise InvalidOrderError(f"Order.side must be a Side, got {type(self.side).__name__}")
        if not isinstance(self.status, OrderStatus):
            raise InvalidOrderError(f"Order.status must be an OrderStatus, got {type(self.status).__name__}")
        if self.order_type != LIMIT_ORDER_TYPE:
            raise InvalidOrderError(f"Order.order_type must be {LIMIT_ORDER_TYPE!r}, got {self.order_type!r}")

        for field in ("price", "quantity", "filled_quantity", "avg_fill_price"):
            value = float(getattr(self, field))
            if not math.isfinite(value) or value < 0.0:
                raise InvalidOrderError(f"Order.{field} must be a non-negative finite number, got {value!r}")
        if self.price <= 0.0:
            raise InvalidOrderError("Order.price must be > 0")
        if self.quantity <= 0.0:
            raise InvalidOrderError("Order.quantity must be > 0")
        if self.filled_quantity > self.quantity + 1e-9:
            raise InvalidOrderError(
                f"Order.filled_quantity {self.filled_quantity} cannot exceed quantity {self.quantity}"
            )
        if self.filled_quantity > 0.0 and self.avg_fill_price <= 0.0:
            raise InvalidOrderError("Order.avg_fill_price must be > 0 once fills exist")
        for field in ("created_at", "updated_at"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise InvalidOrderError(f"Order.{field} must be a non-negative int epoch-millisecond value")
        if self.correlation is not None and not isinstance(self.correlation, OrderCorrelation):
            raise InvalidOrderError("Order.correlation must be an OrderCorrelation")

    # ------------------------------------------------------------------ 派生

    @property
    def remaining_quantity(self) -> float:
        return max(0.0, self.quantity - self.filled_quantity)

    @property
    def executed_quantity(self) -> float:
        """终态之后仍可能被 late fill 更新，因此单独暴露。"""
        return self.final_executed_quantity if self.status.is_terminal and self.final_executed_quantity is not None else self.filled_quantity

    @property
    def notional(self) -> float:
        """按订单自身价格计的未成交名义价值（pending exposure 用最坏情形）。"""
        return self.remaining_quantity * self.price

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal

    @property
    def is_active(self) -> bool:
        return self.status.is_active

    @property
    def is_lost(self) -> bool:
        return self.status.is_lost

    def view(self) -> dict[str, object]:
        """审计/telemetry 用的扁平视图。"""
        return {
            "client_order_id": self.client_order_id,
            "exchange_order_id": self.exchange_order_id,
            "venue": self.venue.value,
            "symbol": self.symbol,
            "side": self.side.value,
            "type": self.order_type,
            "price": self.price,
            "quantity": self.quantity,
            "filled_quantity": self.filled_quantity,
            "avg_fill_price": self.avg_fill_price,
            "reduce_only": self.reduce_only,
            "post_only": self.post_only,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "correlation": None if self.correlation is None else self.correlation.view(),
        }

    def with_status(self, status: OrderStatus, *, timestamp: Milliseconds, **changes: object) -> Order:
        """返回替换后的订单（记录终态时刻的累计成交量）。"""
        updates: dict[str, object] = {"status": status, "updated_at": timestamp}
        if status.is_terminal:
            # 终态时的累计成交量必须以**本次**转换后的 filled_quantity 为准
            updates.setdefault("final_executed_quantity", float(changes.get("filled_quantity", self.filled_quantity)))
            updates.setdefault("lost_reason", self.lost_reason)
        updates.update(changes)
        return replace(self, **updates)


@dataclass(frozen=True, slots=True)
class ExternalOrder:
    """交易所侧订单事实（reconciliation 输入，由 adapter 提供）。

    `side` / `quantity` / `price` 是 adopt 未知订单所必需的；缺失时 reconciliation 会
    fail closed（`ADOPT_REJECTED`）。
    """

    client_order_id: str
    exchange_order_id: str | None
    symbol: str
    status: OrderStatus
    filled_quantity: float
    avg_fill_price: float = 0.0
    side: Side | None = None
    quantity: float | None = None
    price: float | None = None


@dataclass(frozen=True, slots=True)
class ExternalFill:
    """交易所侧成交事实（reconciliation 输入）。"""

    client_order_id: str
    execution_id: str
    trade_id: str
    price: float
    quantity: float
    timestamp: Milliseconds
    fee: float = 0.0
    fee_asset: str = "USDT"


__all__ = [
    "ALLOWED_TRANSITIONS",
    "LIMIT_ORDER_TYPE",
    "ExecutionError",
    "ExternalFill",
    "ExternalOrder",
    "IllegalOrderTransition",
    "InvalidOrderError",
    "Order",
    "OrderNotFoundError",
    "OrderStatus",
    "can_transition",
]
