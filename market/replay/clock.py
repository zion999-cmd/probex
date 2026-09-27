"""Replay 逻辑时钟。

只表达逻辑时间，不做调度：

- 逻辑时间按事件推进，绝不使用 `wall-clock`，也绝不 `sleep`。
- 只前进不后退：`advance_to` 收到不晚于当前逻辑时间的时间戳时保持不变。
  这样即使事件时间戳相同或轻微乱序，Replay 仍然确定。
"""

from __future__ import annotations

from market.events.types import Milliseconds


def _require_time(value: object, *, field: str) -> Milliseconds:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an int epoch-millisecond value")
    if value < 0:
        raise ValueError(f"{field} must be >= 0, got {value}")
    return value


class ReplayClock:
    """Replay 期间的下游时钟。"""

    def __init__(self, *, start: Milliseconds = 0) -> None:
        self._start = _require_time(start, field="start")
        self._now = self._start

    def now(self) -> Milliseconds:
        """当前逻辑时间。"""
        return self._now

    def advance_to(self, event_time: Milliseconds) -> Milliseconds:
        """把逻辑时间推进到 `event_time`（若更晚），返回推进后的逻辑时间。"""
        event_time = _require_time(event_time, field="event_time")
        if event_time > self._now:
            self._now = event_time
        return self._now

    def reset(self) -> None:
        """回到初始逻辑时间。"""
        self._now = self._start
