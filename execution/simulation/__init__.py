"""Event-level Fill Simulation（P0001.8）。

`SimulatedVenue` 用真实 Replay 的 L2 Book + aggressor trade 事件推断 Maker 成交，
实现 `ExecutionAdapter` 契约；`PaperBroker` 保持原有职责不变（可控单元 / 故障测试）。
"""

from __future__ import annotations

from execution.simulation.fees import FeeSchedule
from execution.simulation.latency import LatencyModel
from execution.simulation.queue import UNKNOWN_QUEUE, QueueEstimate, clear_queue, consume_queue, queue_from_visible
from execution.simulation.types import (
    FillInferenceState,
    FillReason,
    Liquidity,
    QueueState,
    RestingOrderView,
    SimulatedFill,
    SimulatedRejectReason,
    SimulationError,
)
from execution.simulation.venue import DEFAULT_EXCHANGE_ID_PREFIX, SimulatedVenue

__all__ = [
    "DEFAULT_EXCHANGE_ID_PREFIX",
    "FeeSchedule",
    "FillInferenceState",
    "FillReason",
    "LatencyModel",
    "Liquidity",
    "QueueEstimate",
    "QueueState",
    "RestingOrderView",
    "SimulatedFill",
    "SimulatedRejectReason",
    "SimulatedVenue",
    "SimulationError",
    "UNKNOWN_QUEUE",
    "clear_queue",
    "consume_queue",
    "queue_from_visible",
]
