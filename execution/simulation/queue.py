"""Maker 队列近似（P0001.8 §3 / §4 / §5 / §6）。

模型（刻意保守）：

```text
订单到达某价位时：queue_ahead = 该价位当前可见 resting quantity
```

之后**只有**有明确成交证据的 aggressor trade 才推进队列 —— L2 quantity 下降**不**推进
（减少可能来自 cancel / modify / hidden liquidity / feed artifact，§4）。

`QueueState.UNKNOWN` 时 `queue_ahead` 为 `None`（未知 ≠ 0），且**不得**产生推测性成交（§8 / §9）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from execution.simulation.types import QueueState, SimulationError


@dataclass(frozen=True, slots=True)
class QueueEstimate:
    """前方队列估计。`KNOWN` 必须有 `ahead`，`UNKNOWN` 必须没有。"""

    state: QueueState
    ahead: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, QueueState):
            raise SimulationError(f"QueueEstimate.state must be a QueueState, got {type(self.state).__name__}")
        if self.state is QueueState.UNKNOWN:
            if self.ahead is not None:
                raise SimulationError("QueueEstimate.ahead must be None when the queue state is UNKNOWN")
            return
        if self.ahead is None:
            raise SimulationError("QueueEstimate.ahead is required when the queue state is KNOWN")
        value = float(self.ahead)
        if not math.isfinite(value) or value < 0.0:
            raise SimulationError(f"QueueEstimate.ahead must be a non-negative finite number, got {self.ahead!r}")
        object.__setattr__(self, "ahead", value)

    @property
    def is_known(self) -> bool:
        return self.state is QueueState.KNOWN

    @property
    def is_unknown(self) -> bool:
        return self.state is QueueState.UNKNOWN


#: 未建立的队列估计。
UNKNOWN_QUEUE = QueueEstimate(state=QueueState.UNKNOWN)


def queue_from_visible(visible_size: float | None) -> QueueEstimate:
    """由 L2 可见数量建立队列估计。

    `visible_size` 为 `None`、非正或非有限（价位不可观察 / 已被移除）时返回 `UNKNOWN`：
    绝不能把「看不到」当作 `queue_ahead = 0`（§8）。
    """
    if visible_size is None or isinstance(visible_size, bool) or not isinstance(visible_size, (int, float)):
        return UNKNOWN_QUEUE
    value = float(visible_size)
    if not math.isfinite(value) or value <= 0.0:
        return UNKNOWN_QUEUE
    return QueueEstimate(state=QueueState.KNOWN, ahead=value)


def consume_queue(queue: QueueEstimate, volume: float) -> tuple[QueueEstimate, float]:
    """用一笔 aggressor 成交量推进队列。

    返回 `(新队列, 可用于成交的数量)`：先消耗 `queue_ahead`，剩余部分才轮到我们（§5）。
    `UNKNOWN` 队列不消耗也不成交（返回 `(UNKNOWN_QUEUE, 0.0)`）。
    """
    if isinstance(volume, bool) or not isinstance(volume, (int, float)):
        raise SimulationError("consume_queue(volume) must be a number")
    amount = float(volume)
    if not math.isfinite(amount) or amount < 0.0:
        raise SimulationError(f"consume_queue(volume) must be a non-negative finite number, got {volume!r}")
    if queue.is_unknown:
        return UNKNOWN_QUEUE, 0.0
    assert queue.ahead is not None  # KNOWN ⇒ ahead 非 None
    consumed = min(queue.ahead, amount)
    return QueueEstimate(state=QueueState.KNOWN, ahead=queue.ahead - consumed), amount - consumed


def clear_queue(queue: QueueEstimate) -> QueueEstimate:
    """trade-through：该档位已被穿过，队列视为清空（仍为 `KNOWN`，ahead = 0）。"""
    if queue.is_unknown:
        raise SimulationError("cannot clear an UNKNOWN queue: its state must stay UNKNOWN")
    return QueueEstimate(state=QueueState.KNOWN, ahead=0.0)


__all__ = ["UNKNOWN_QUEUE", "QueueEstimate", "clear_queue", "consume_queue", "queue_from_visible"]
