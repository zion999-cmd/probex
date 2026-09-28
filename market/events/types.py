"""统一市场事件边界。

Live 与 Replay 在进入 Market Core 之前必须统一为 `MarketEvent`（CLAUDE.md / P0001.1 §1.2）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias

from market.events.errors import InvalidEventError
from market.events.payloads import BookDeltaPayload, BookSnapshotPayload, TradePayload

#: Unix epoch 毫秒（UTC）。交易所报文本身使用毫秒。
Milliseconds: TypeAlias = int


class Venue(Enum):
    """交易场所。P0001.1 只归一化 Binance。"""

    BINANCE = "binance"


class EventType(Enum):
    """事件词表。

    这是 P0001.1 定义的统一 MarketEvent 契约。`BOOK_SNAPSHOT` / `BOOK_DELTA` 由 P0001.1 定义，
    `TRADE` 由 P0001.8 补齐载荷（供 event-level fill simulation 使用）；
    其余取值仍由后续阶段补齐载荷。
    """

    BOOK_SNAPSHOT = "book_snapshot"
    BOOK_DELTA = "book_delta"
    TRADE = "trade"
    TICKER = "ticker"
    BAR = "bar"
    FUNDING = "funding"
    MARK_PRICE = "mark_price"


#: 已具备载荷类型的事件载荷联合。
MarketPayload: TypeAlias = BookSnapshotPayload | BookDeltaPayload | TradePayload

_PAYLOAD_EVENT_TYPES: dict[type, EventType] = {
    BookSnapshotPayload: EventType.BOOK_SNAPSHOT,
    BookDeltaPayload: EventType.BOOK_DELTA,
    TradePayload: EventType.TRADE,
}

#: 必须携带 sequence 的事件类型。
SEQUENCED_EVENT_TYPES: frozenset[EventType] = frozenset(
    {EventType.BOOK_SNAPSHOT, EventType.BOOK_DELTA}
)


def event_type_for(payload: MarketPayload) -> EventType:
    """返回载荷对应的事件类型；未知载荷视为非法边界输入。"""
    try:
        return _PAYLOAD_EVENT_TYPES[type(payload)]
    except KeyError:
        raise InvalidEventError(f"unsupported payload type: {type(payload).__name__}") from None


def _require_timestamp(value: object, *, field: str) -> Milliseconds:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidEventError(f"MarketEvent.{field} must be an int epoch-millisecond value")
    if value < 0:
        raise InvalidEventError(f"MarketEvent.{field} must be >= 0, got {value}")
    return value


@dataclass(frozen=True, slots=True)
class MarketEvent:
    """跨 Live / Replay 的统一市场事件。

    - `exchange_ts`：交易所事件时间。
    - `receive_ts`：本地接收时间。
    - `process_ts`：进入处理管道的时间。
    - `sequence`：交易所序号（book 事件使用 `last_update_id`，必须与载荷一致）。
    """

    venue: Venue
    symbol: str
    event_type: EventType
    exchange_ts: Milliseconds
    receive_ts: Milliseconds
    process_ts: Milliseconds
    sequence: int | None
    payload: MarketPayload

    def __post_init__(self) -> None:
        if not isinstance(self.venue, Venue):
            raise InvalidEventError(f"MarketEvent.venue must be a Venue, got {type(self.venue).__name__}")
        if not isinstance(self.symbol, str) or not self.symbol:
            raise InvalidEventError("MarketEvent.symbol must be a non-empty string")
        if not isinstance(self.event_type, EventType):
            raise InvalidEventError(f"MarketEvent.event_type must be an EventType, got {type(self.event_type).__name__}")

        for field in ("exchange_ts", "receive_ts", "process_ts"):
            _require_timestamp(getattr(self, field), field=field)

        expected = event_type_for(self.payload)
        if self.event_type is not expected:
            raise InvalidEventError(
                f"MarketEvent.event_type {self.event_type.value!r} does not match payload "
                f"{type(self.payload).__name__} ({expected.value!r})"
            )

        if self.event_type in SEQUENCED_EVENT_TYPES:
            if self.sequence is None:
                raise InvalidEventError(f"MarketEvent.sequence is required for {self.event_type.value}")
            if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
                raise InvalidEventError("MarketEvent.sequence must be an int")
            if self.sequence != self.payload.last_update_id:
                raise InvalidEventError(
                    f"MarketEvent.sequence ({self.sequence}) must equal payload.last_update_id "
                    f"({self.payload.last_update_id})"
                )
        elif self.sequence is not None:
            raise InvalidEventError(f"MarketEvent.sequence must be None for {self.event_type.value}")
