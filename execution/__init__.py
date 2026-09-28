"""执行领域：订单生命周期、事件驱动的成交记账与 Paper Execution。

分层：`OrderProposal → ExecutionEngine(RiskGate) → OrderManager → ExecutionAdapter(PaperBroker)`
→ `ExecutionEvent → OrderTracker → canonical Fill → AccountingCore`。

本模块不 import Prediction / Jev / Strategy（SC-16）。
"""

from __future__ import annotations

from execution.adapters.base import ExecutionAdapter
from execution.adapters.paper import PaperBroker, PaperRejectReason
from execution.engine import ExecutionEngine, ExecutionRejected, ExecutionResult
from execution.events import (
    EVENT_TYPES,
    ExecutionEvent,
    ExecutionEventType,
    FillReceived,
    OrderAccepted,
    OrderCanceled,
    OrderExpired,
    OrderRejected,
    OrderStatusUpdate,
)
from execution.manager import OrderManager, ReplaceOutcome
from execution.reconciliation import (
    ReconciliationAction,
    ReconciliationActionKind,
    ReconciliationReport,
    reconcile,
)
from execution.tracker import (
    CLIENT_ORDER_ID_PREFIX,
    ExecutionEventOutcome,
    OrderTracker,
    TrackerUpdate,
)
from execution.types import (
    ALLOWED_TRANSITIONS,
    LIMIT_ORDER_TYPE,
    ExecutionError,
    ExternalFill,
    ExternalOrder,
    IllegalOrderTransition,
    InvalidOrderError,
    Order,
    OrderNotFoundError,
    OrderStatus,
)

__all__ = [
    "ALLOWED_TRANSITIONS",
    "CLIENT_ORDER_ID_PREFIX",
    "EVENT_TYPES",
    "LIMIT_ORDER_TYPE",
    "ExecutionAdapter",
    "ExecutionEngine",
    "ExecutionError",
    "ExecutionEvent",
    "ExecutionEventOutcome",
    "ExecutionEventType",
    "ExecutionRejected",
    "ExecutionResult",
    "ExternalFill",
    "ExternalOrder",
    "FillReceived",
    "IllegalOrderTransition",
    "InvalidOrderError",
    "Order",
    "OrderAccepted",
    "OrderCanceled",
    "OrderExpired",
    "OrderManager",
    "OrderNotFoundError",
    "OrderRejected",
    "OrderStatus",
    "OrderStatusUpdate",
    "OrderTracker",
    "PaperBroker",
    "PaperRejectReason",
    "ReconciliationAction",
    "ReconciliationActionKind",
    "ReconciliationReport",
    "ReplaceOutcome",
    "TrackerUpdate",
    "reconcile",
]
