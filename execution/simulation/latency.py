"""LatencyModel：显式提交 / 撤单延迟（P0001.8 §9 / §10）。

语义（边界都是**闭区间的下界**，即 `event_time >= 生效时刻` 才生效）：

- 订单在 `submitted_at + submit_latency_ms` **之后**进入模拟盘口；在此之前到达的
  市场事件**不属于**该订单（§9）。
- 撤单在 `requested_at + cancel_latency_ms` 才生效；窗口内订单仍可成交，
  生效之后禁止任何新成交（§10）。**同刻平局时撤单优先**（对成交保守）。

两个延迟都必须显式注入，无生产默认值（CLAUDE.md §12）。
"""

from __future__ import annotations

from dataclasses import dataclass

from execution.simulation.types import SimulationError
from market.events.types import Milliseconds


def _require_ms(value: object, *, field: str) -> Milliseconds:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SimulationError(f"{field} must be an int epoch-millisecond value >= 0, got {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class LatencyModel:
    """订单生效延迟模型。"""

    submit_latency_ms: Milliseconds
    cancel_latency_ms: Milliseconds

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "submit_latency_ms", _require_ms(self.submit_latency_ms, field="LatencyModel.submit_latency_ms")
        )
        object.__setattr__(
            self, "cancel_latency_ms", _require_ms(self.cancel_latency_ms, field="LatencyModel.cancel_latency_ms")
        )

    def entry_ts(self, submitted_at: Milliseconds) -> Milliseconds:
        """订单进入模拟盘口的时刻。"""
        return _require_ms(submitted_at, field="submitted_at") + self.submit_latency_ms

    def cancel_effective_ts(self, requested_at: Milliseconds) -> Milliseconds:
        """撤单生效时刻。"""
        return _require_ms(requested_at, field="requested_at") + self.cancel_latency_ms


__all__ = ["LatencyModel"]
