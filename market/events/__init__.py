"""统一市场事件边界模块。"""

from __future__ import annotations

from market.events.errors import InvalidEventError, InvalidPayloadError, MarketEventError
from market.events.payloads import (
    AggressorSide,
    BookDeltaPayload,
    BookSnapshotPayload,
    PriceLevel,
    TradePayload,
)
from market.events.types import (
    SEQUENCED_EVENT_TYPES,
    EventType,
    MarketEvent,
    MarketPayload,
    Milliseconds,
    Venue,
    event_type_for,
)

__all__ = [
    "SEQUENCED_EVENT_TYPES",
    "AggressorSide",
    "BookDeltaPayload",
    "BookSnapshotPayload",
    "EventType",
    "InvalidEventError",
    "InvalidPayloadError",
    "MarketEvent",
    "MarketEventError",
    "MarketPayload",
    "Milliseconds",
    "PriceLevel",
    "TradePayload",
    "Venue",
    "event_type_for",
]
