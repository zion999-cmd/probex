"""Event Store：append-only 市场事件持久化。

对外入口是 `EventWriter` / `EventReader` 接口；JSONL 只是当前 adapter，
换格式不应影响 `market/replay`。
"""

from __future__ import annotations

from storage.events.codec import (
    SCHEMA_VERSION,
    EventRecord,
    canonical_json,
    compute_event_id,
    decode_event,
    decode_record,
    dumps_record,
    encode_event,
    encode_record,
    loads_record,
)
from storage.events.errors import (
    EventIntegrityError,
    EventOrdinalError,
    EventStoreError,
    EventStoreFormatError,
    UnsupportedSchemaVersionError,
)
from storage.events.reader import EventReader, JsonlEventReader
from storage.events.writer import EventWriter, JsonlEventWriter

__all__ = [
    "SCHEMA_VERSION",
    "EventIntegrityError",
    "EventOrdinalError",
    "EventReader",
    "EventRecord",
    "EventStoreError",
    "EventStoreFormatError",
    "EventWriter",
    "JsonlEventReader",
    "JsonlEventWriter",
    "UnsupportedSchemaVersionError",
    "canonical_json",
    "compute_event_id",
    "decode_event",
    "decode_record",
    "dumps_record",
    "encode_event",
    "encode_record",
    "loads_record",
]
