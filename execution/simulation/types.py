"""Event-level Fill Simulation 的类型契约（P0001.8）。

设计要点（proposals/P0001.8 §13）：

- **不制造虚假的 queue 精度**：`QueueState.KNOWN` / `UNKNOWN` 是显式状态，`UNKNOWN` 时
  `queue_ahead` 为 `None`（不是 0）；
- 每条模拟成交都带完整证据：`fill_reason`、`queue_state`、`event_ordinal`、`aggregate_trade_id`；
- 模拟成交一律 `Liquidity.MAKER`（本阶段不实现 taker）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from execution.types import OrderStatus
from market.events.types import Milliseconds
from portfolio.types import Side


class SimulationError(RuntimeError):
    """Fill simulation 的契约错误。"""


class QueueState(Enum):
    """订单前方队列的估计状态。"""

    #: 已由可观察的 L2 档位建立估计（仅由 aggressor trade 推进）。
    KNOWN = "known"
    #: 无法建立或已被作废：**不得**产生推测性成交。
    UNKNOWN = "unknown"


class FillInferenceState(Enum):
    """成交推断是否可用（§7：盘口不可信时必须挂起）。"""

    ACTIVE = "active"
    #: `BookHealth != HEALTHY`（gap / stale / 未同步）→ 停止推导新的 maker 成交。
    SUSPENDED = "suspended"


class FillReason(Enum):
    """模拟成交的证据类型（§6：两者必须分开记录）。"""

    #: aggressor 成交恰好发生在我们的价位：先消耗前方队列，再成交。
    QUEUE_CONSUMED = "queue_consumed"
    #: aggressor 成交价格穿过我们的限价：该档位已被清空。
    TRADE_THROUGH = "trade_through"


class Liquidity(Enum):
    """流动性角色。第一版只实现 maker。"""

    MAKER = "maker"


class SimulatedRejectReason(Enum):
    """模拟 venue 的拒绝原因（venue rule；与 PaperBroker 的词表保持一致）。"""

    DUPLICATE_CLIENT_ORDER_ID = "DUPLICATE_CLIENT_ORDER_ID"
    REJECT_POST_ONLY = "REJECT_POST_ONLY"
    CANCEL_UNKNOWN_ORDER = "CANCEL_UNKNOWN_ORDER"


@dataclass(frozen=True, slots=True)
class SimulatedFill:
    """一条模拟成交及其全部证据（审计 / telemetry）。"""

    client_order_id: str
    exchange_order_id: str
    symbol: str
    side: Side
    price: float
    quantity: float
    fee: float
    fee_asset: str
    liquidity: Liquidity
    fill_reason: FillReason
    queue_state: QueueState
    #: 触发该成交的市场事件在模拟器内的序号（0 基，等于喂入顺序）。
    event_ordinal: int
    #: 触发该成交的市场事件的 `exchange_ts`（成交时间）。
    exchange_ts: Milliseconds
    execution_id: str
    trade_id: str
    aggregate_trade_id: int


@dataclass(frozen=True, slots=True)
class RestingOrderView:
    """模拟器内一张挂单的只读投影（研究用 telemetry）。"""

    client_order_id: str
    exchange_order_id: str
    symbol: str
    side: Side
    price: float
    quantity: float
    filled_quantity: float
    status: OrderStatus
    #: 是否已跨过 submit latency 进入模拟盘口。
    entered: bool
    entry_exchange_ts: Milliseconds
    queue_state: QueueState
    queue_ahead: float | None
    initial_queue_ahead: float | None
    fill_inference: FillInferenceState
    #: gap 之后重建队列的次数（> 0 表示该成交的证据经历过信息缺口）。
    queue_rebuild_count: int
    cancel_requested_at: Milliseconds | None
    cancel_effective_ts: Milliseconds | None
    last_event_ordinal: int | None
    fills: tuple[SimulatedFill, ...]

    @property
    def remaining_quantity(self) -> float:
        return max(0.0, self.quantity - self.filled_quantity)


__all__ = [
    "FillInferenceState",
    "FillReason",
    "Liquidity",
    "QueueState",
    "RestingOrderView",
    "SimulatedFill",
    "SimulatedRejectReason",
    "SimulationError",
]
