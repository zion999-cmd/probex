"""窗口基础设施：`TimeSeries` / `TimeWindow` / `EventWindow`。

统一规则（P0001.3 §6）：

- 所有窗口的时间戳都是**显式参数**，来自事件时间（`exchange_ts`）或 `ReplayClock`。
  本模块不得读取 wall-clock（不 import `time`）。
- 观测按时间戳有序保存，因此事件时间轻微乱序也不会破坏查询语义。
- 未知 ≠ 0：空窗口返回 `None`，不返回 `0`。
- 每个 feature 不得自行保存数组，一律使用本模块的窗口。
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right, insort
from collections import deque

from market.events.types import Milliseconds


def _require_timestamp(value: object, *, field: str = "timestamp") -> Milliseconds:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an int epoch-millisecond value")
    if value < 0:
        raise ValueError(f"{field} must be >= 0, got {value}")
    return value


def _require_value(value: object, *, field: str = "value") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number, got {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite, got {value!r}")
    return number


class TimeSeries:
    """受 horizon 约束、按时间戳有序的观测序列。"""

    def __init__(self, horizon_ms: Milliseconds) -> None:
        if horizon_ms <= 0:
            raise ValueError("horizon_ms must be > 0")
        self._horizon_ms = horizon_ms
        self._observations: list[tuple[Milliseconds, float]] = []

    @property
    def horizon_ms(self) -> Milliseconds:
        return self._horizon_ms

    def __len__(self) -> int:
        return len(self._observations)

    @property
    def count(self) -> int:
        return len(self._observations)

    @property
    def oldest_timestamp(self) -> Milliseconds | None:
        return self._observations[0][0] if self._observations else None

    @property
    def newest_timestamp(self) -> Milliseconds | None:
        return self._observations[-1][0] if self._observations else None

    def record(self, timestamp: Milliseconds, value: float) -> bool:
        """记录一次观测。返回 False 表示该观测早于 horizon 起点而被丢弃。"""
        timestamp = _require_timestamp(timestamp)
        number = _require_value(value)
        if self._observations and timestamp < self._observations[-1][0] - self._horizon_ms:
            return False
        insort(self._observations, (timestamp, number))
        self._prune()
        return True

    def value_at_or_before(self, timestamp: Milliseconds) -> float | None:
        """最近一次 `ts <= timestamp` 的观测值；没有则 `None`（永不读取未来）。"""
        timestamp = _require_timestamp(timestamp)
        index = bisect_right(self._observations, (timestamp, math.inf))
        if index == 0:
            return None
        return self._observations[index - 1][1]

    def observations_between(
        self, start_inclusive: Milliseconds, end_inclusive: Milliseconds
    ) -> tuple[float, ...]:
        """返回 `start_inclusive <= ts <= end_inclusive` 的观测值（按时间升序）。

        两端均为闭区间，与 return 的「latest observation <= target」lookup 规则一致。
        """
        start_inclusive = _require_timestamp(start_inclusive, field="start_inclusive")
        end_inclusive = _require_timestamp(end_inclusive, field="end_inclusive")
        left = bisect_left(self._observations, (start_inclusive,))
        right = bisect_right(self._observations, (end_inclusive, math.inf))
        return tuple(value for _, value in self._observations[left:right])

    def coverage_ms(self, at: Milliseconds) -> Milliseconds:
        """在 `at` 时刻，序列覆盖的时长（`at - oldest`，无观测时为 0）。"""
        at = _require_timestamp(at, field="at")
        oldest = self.oldest_timestamp
        if oldest is None:
            return 0
        return max(0, at - oldest)

    def reset(self) -> None:
        self._observations.clear()

    def _prune(self) -> None:
        cutoff = self._observations[-1][0] - self._horizon_ms
        index = bisect_left(self._observations, (cutoff,))
        if index > 0:
            del self._observations[:index]


class TimeWindow:
    """时间窗：`[t - length, t]` 内的观测（两端闭区间）。"""

    def __init__(self, length_ms: Milliseconds) -> None:
        self._length_ms = length_ms
        self._series = TimeSeries(horizon_ms=length_ms)

    @property
    def length_ms(self) -> Milliseconds:
        return self._length_ms

    @property
    def oldest_timestamp(self) -> Milliseconds | None:
        return self._series.oldest_timestamp

    @property
    def newest_timestamp(self) -> Milliseconds | None:
        return self._series.newest_timestamp

    def observe(self, value: float, timestamp: Milliseconds) -> bool:
        return self._series.record(timestamp, value)

    def values(self, at: Milliseconds) -> tuple[float, ...]:
        return self._series.observations_between(at - self._length_ms, at)

    def count(self, at: Milliseconds) -> int:
        return len(self.values(at))

    def sum(self, at: Milliseconds) -> float | None:
        """窗口内观测之和；窗口为空时返回 `None`（未知 ≠ 0）。"""
        values = self.values(at)
        if not values:
            return None
        return math.fsum(values)

    def reset(self) -> None:
        self._series.reset()


class EventWindow:
    """事件窗：最近 `capacity` 次观测，与时间无关。

    与 `TimeWindow` 共同构成提案要求的窗口基础设施。本阶段所有已定义 feature 都是
    时间窗（OFI / return / volatility），事件窗由后续阶段消费。
    """

    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        self._capacity = capacity
        self._values: deque[float] = deque(maxlen=capacity)
        self._timestamps: deque[Milliseconds] = deque(maxlen=capacity)

    @property
    def capacity(self) -> int:
        return self._capacity

    def __len__(self) -> int:
        return len(self._values)

    @property
    def count(self) -> int:
        return len(self._values)

    @property
    def oldest_timestamp(self) -> Milliseconds | None:
        return self._timestamps[0] if self._timestamps else None

    @property
    def newest_timestamp(self) -> Milliseconds | None:
        return self._timestamps[-1] if self._timestamps else None

    def observe(self, value: float, timestamp: Milliseconds) -> None:
        self._values.append(_require_value(value))
        self._timestamps.append(_require_timestamp(timestamp))

    def values(self) -> tuple[float, ...]:
        return tuple(self._values)

    def sum(self) -> float | None:
        if not self._values:
            return None
        return math.fsum(self._values)

    def mean(self) -> float | None:
        if not self._values:
            return None
        return math.fsum(self._values) / len(self._values)

    def coverage_ms(self) -> Milliseconds:
        if len(self._timestamps) < 2:
            return 0
        return self._timestamps[-1] - self._timestamps[0]

    def reset(self) -> None:
        self._values.clear()
        self._timestamps.clear()
