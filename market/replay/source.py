"""ReplaySource：把 Event Store 中的记录还原成 canonical `MarketEvent` 流。

设计约束：

- Replay 消费原来的 `MarketEvent`，不存在第二套 ReplayEvent。
- 事件顺序完全由 store 的 recorded ordinal 决定（见 `EventReader` 契约）；
  时间戳不参与排序，`sequence` 由下游 `MarketBook` 做连续性判定。
- 每消费一条事件，逻辑时钟推进到该事件的 `exchange_ts`。
- 模式决定入口：`FULL` 用 `iter_events()`，`STEP` 用 `next_event()`；
  用错入口立即报错，避免「FULL 被误当 STEP」这类静默行为差异。
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from enum import Enum

from market.events.types import MarketEvent
from market.replay.clock import ReplayClock
from storage.events.codec import EventRecord


class ReplayMode(Enum):
    """Replay 模式。"""

    #: 一次跑完整个数据集。
    FULL = "full"
    #: 每次只消费一条事件，由调用方决定何时继续。
    STEP = "step"


class ReplayModeError(RuntimeError):
    """使用了与当前模式不匹配的入口。"""


class ReplaySource:
    """按 canonical 顺序产出 `MarketEvent` 的事件源。"""

    def __init__(
        self,
        reader: Iterable[EventRecord],
        *,
        mode: ReplayMode = ReplayMode.FULL,
        clock: ReplayClock | None = None,
    ) -> None:
        if not isinstance(mode, ReplayMode):
            raise ValueError(f"mode must be a ReplayMode, got {type(mode).__name__}")
        self._reader = reader
        self._mode = mode
        self._clock = clock if clock is not None else ReplayClock()
        self._records: Iterator[EventRecord] = iter(reader)

    @property
    def mode(self) -> ReplayMode:
        return self._mode

    @property
    def clock(self) -> ReplayClock:
        return self._clock

    def iter_events(self) -> Iterator[MarketEvent]:
        """FULL 模式入口：产出全部剩余事件。"""
        self._require_mode(ReplayMode.FULL, "iter_events")
        return self._iter_events()

    def next_event(self) -> MarketEvent | None:
        """STEP 模式入口：消费一条事件；数据集结束后返回 `None`。"""
        self._require_mode(ReplayMode.STEP, "next_event")
        record = next(self._records, None)
        if record is None:
            return None
        return self._consume(record)

    def reset(self) -> None:
        """回到数据集开头，并重置逻辑时钟。要求 reader 可重复迭代。"""
        self._records = iter(self._reader)
        self._clock.reset()

    def _iter_events(self) -> Iterator[MarketEvent]:
        for record in self._records:
            yield self._consume(record)

    def _consume(self, record: EventRecord) -> MarketEvent:
        self._clock.advance_to(record.event.exchange_ts)
        return record.event

    def _require_mode(self, expected: ReplayMode, entry_point: str) -> None:
        if self._mode is not expected:
            raise ReplayModeError(
                f"{entry_point}() is only available in {expected.value} mode, current mode is {self._mode.value}"
            )
