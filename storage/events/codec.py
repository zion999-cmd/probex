"""Event Store 记录编解码。

一条记录 = 一行 JSON（JSONL adapter），字段固定：

```text
schema_version  记录 schema 版本，当前为 1
ordinal         store 分配的稳定序号，严格递增，回放顺序的唯一依据
event_id        事件内容摘要（sha256），用于检测历史被原地修改
source          可选 capture metadata
event           canonical MarketEvent（8 字段）
```

规范：

- 编码是 canonical 的：键排序、无多余空白、禁止 NaN / Infinity，因此同一事件字节级稳定。
- 解码严格：缺失字段、类型不符、未知取值、`MarketEvent` 不变量失败、`event_id` 不匹配，一律抛出
  `EventStoreError` 子类（fail closed）。
- `event_id` 由事件内容派生，不含 ordinal，因此可跨 store 校验内容是否被篡改。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from market.events.errors import MarketEventError
from market.events.payloads import BookDeltaPayload, BookSnapshotPayload, PriceLevel
from market.events.types import EventType, MarketEvent, Milliseconds, Venue

from storage.events.errors import (
    EventIntegrityError,
    EventStoreFormatError,
    UnsupportedSchemaVersionError,
)

#: 当前 Event Store schema 版本。
SCHEMA_VERSION = 1

#: `event_id` 前缀。
_EVENT_ID_PREFIX = "sha256:"

_EVENT_TYPES_BY_VALUE: dict[str, EventType] = {event_type.value: event_type for event_type in EventType}
_VENUES_BY_VALUE: dict[str, Venue] = {venue.value: venue for venue in Venue}


@dataclass(frozen=True, slots=True)
class EventRecord:
    """Event Store 中的一条记录。"""

    ordinal: int
    event_id: str
    schema_version: int
    event: MarketEvent
    source: str | None = None


def canonical_json(value: object) -> str:
    """canonical JSON：键排序、紧凑分隔符、禁止非有限数值。"""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise EventStoreFormatError(f"value is not canonically serializable: {exc}") from exc


def compute_event_id(event: MarketEvent) -> str:
    """由事件内容派生稳定的 `event_id`。"""
    digest = hashlib.sha256(canonical_json(encode_event(event)).encode("utf-8")).hexdigest()
    return f"{_EVENT_ID_PREFIX}{digest}"


def encode_event(event: MarketEvent) -> dict[str, object]:
    """把 `MarketEvent` 编码为 JSON 友好结构。"""
    if not isinstance(event, MarketEvent):
        raise EventStoreFormatError(f"expected a MarketEvent, got {type(event).__name__}")
    return {
        "venue": event.venue.value,
        "symbol": event.symbol,
        "event_type": event.event_type.value,
        "exchange_ts": event.exchange_ts,
        "receive_ts": event.receive_ts,
        "process_ts": event.process_ts,
        "sequence": event.sequence,
        "payload": _encode_payload(event.payload),
    }


def encode_record(record: EventRecord) -> dict[str, object]:
    """把 `EventRecord` 编码为 JSON 友好结构。"""
    encoded: dict[str, object] = {
        "schema_version": record.schema_version,
        "ordinal": record.ordinal,
        "event_id": record.event_id,
        "event": encode_event(record.event),
    }
    if record.source is not None:
        encoded["source"] = record.source
    return encoded


def dumps_record(record: EventRecord) -> str:
    """序列化为一行 JSONL（不含换行符）。"""
    return canonical_json(encode_record(record))


def loads_record(line: str) -> EventRecord:
    """解析一行 JSONL。"""
    if not isinstance(line, str):
        raise EventStoreFormatError(f"record line must be a string, got {type(line).__name__}")
    text = line.strip()
    if not text:
        raise EventStoreFormatError("record line is empty")
    try:
        raw = json.loads(text, parse_constant=_reject_json_constant)
    except json.JSONDecodeError as exc:
        raise EventStoreFormatError(f"record line is not valid JSON: {exc}") from exc
    return decode_record(raw)


def decode_record(raw: object) -> EventRecord:
    """从 JSON 结构还原 `EventRecord`，并校验 schema、ordinal 与事件完整性。"""
    message = _require_object(raw, path="record")
    schema_version = _require_int(
        _require_field(message, "schema_version", path="record"), path="record.schema_version"
    )
    if schema_version != SCHEMA_VERSION:
        raise UnsupportedSchemaVersionError(
            f"record.schema_version: unsupported version {schema_version}, supported: {SCHEMA_VERSION}"
        )

    ordinal = _require_int(_require_field(message, "ordinal", path="record"), path="record.ordinal")
    if ordinal < 0:
        raise EventStoreFormatError(f"record.ordinal must be >= 0, got {ordinal}")

    event_id = _require_str(_require_field(message, "event_id", path="record"), path="record.event_id")
    source = _require_optional_str(message.get("source"), path="record.source")
    event = decode_event(_require_field(message, "event", path="record"))

    expected_event_id = compute_event_id(event)
    if event_id != expected_event_id:
        raise EventIntegrityError(
            f"record.event_id ({event_id}) does not match event content ({expected_event_id})"
        )

    return EventRecord(
        ordinal=ordinal,
        event_id=event_id,
        schema_version=schema_version,
        event=event,
        source=source,
    )


def decode_event(raw: object) -> MarketEvent:
    """从 JSON 结构还原 canonical `MarketEvent`。"""
    message = _require_object(raw, path="record.event")
    venue = _decode_venue(_require_field(message, "venue", path="record.event"), path="record.event.venue")
    symbol = _require_str(_require_field(message, "symbol", path="record.event"), path="record.event.symbol")
    event_type = _decode_event_type(
        _require_field(message, "event_type", path="record.event"), path="record.event.event_type"
    )
    exchange_ts = _require_milliseconds(
        _require_field(message, "exchange_ts", path="record.event"), path="record.event.exchange_ts"
    )
    receive_ts = _require_milliseconds(
        _require_field(message, "receive_ts", path="record.event"), path="record.event.receive_ts"
    )
    process_ts = _require_milliseconds(
        _require_field(message, "process_ts", path="record.event"), path="record.event.process_ts"
    )
    sequence = _require_optional_int(
        _require_field(message, "sequence", path="record.event"), path="record.event.sequence"
    )
    payload = _decode_payload(_require_field(message, "payload", path="record.event"), path="record.event.payload")

    try:
        return MarketEvent(
            venue=venue,
            symbol=symbol,
            event_type=event_type,
            exchange_ts=exchange_ts,
            receive_ts=receive_ts,
            process_ts=process_ts,
            sequence=sequence,
            payload=payload,
        )
    except MarketEventError as exc:
        raise EventStoreFormatError(f"record.event: {exc}") from exc


def _encode_payload(payload: BookSnapshotPayload | BookDeltaPayload) -> dict[str, object]:
    if isinstance(payload, BookSnapshotPayload):
        return {
            "kind": EventType.BOOK_SNAPSHOT.value,
            "last_update_id": payload.last_update_id,
            "bids": _encode_levels(payload.bids),
            "asks": _encode_levels(payload.asks),
        }
    if isinstance(payload, BookDeltaPayload):
        return {
            "kind": EventType.BOOK_DELTA.value,
            "first_update_id": payload.first_update_id,
            "last_update_id": payload.last_update_id,
            "bids": _encode_levels(payload.bids),
            "asks": _encode_levels(payload.asks),
        }
    raise EventStoreFormatError(f"unsupported payload type: {type(payload).__name__}")


def _encode_levels(levels: Sequence[PriceLevel]) -> list[list[float]]:
    return [[level.price, level.size] for level in levels]


def _decode_payload(raw: object, *, path: str) -> BookSnapshotPayload | BookDeltaPayload:
    message = _require_object(raw, path=path)
    kind = _require_str(_require_field(message, "kind", path=path), path=f"{path}.kind")

    if kind == EventType.BOOK_SNAPSHOT.value:
        last_update_id = _require_int(
            _require_field(message, "last_update_id", path=path), path=f"{path}.last_update_id"
        )
        bids = _decode_levels(_require_field(message, "bids", path=path), path=f"{path}.bids")
        asks = _decode_levels(_require_field(message, "asks", path=path), path=f"{path}.asks")
        try:
            return BookSnapshotPayload(last_update_id=last_update_id, bids=bids, asks=asks)
        except MarketEventError as exc:
            raise EventStoreFormatError(f"{path}: {exc}") from exc

    if kind == EventType.BOOK_DELTA.value:
        first_update_id = _require_int(
            _require_field(message, "first_update_id", path=path), path=f"{path}.first_update_id"
        )
        last_update_id = _require_int(
            _require_field(message, "last_update_id", path=path), path=f"{path}.last_update_id"
        )
        bids = _decode_levels(_require_field(message, "bids", path=path), path=f"{path}.bids")
        asks = _decode_levels(_require_field(message, "asks", path=path), path=f"{path}.asks")
        try:
            return BookDeltaPayload(
                first_update_id=first_update_id,
                last_update_id=last_update_id,
                bids=bids,
                asks=asks,
            )
        except MarketEventError as exc:
            raise EventStoreFormatError(f"{path}: {exc}") from exc

    raise EventStoreFormatError(f"{path}.kind: unknown payload kind {kind!r}")


def _decode_levels(raw: object, *, path: str) -> tuple[PriceLevel, ...]:
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise EventStoreFormatError(f"{path}: expected an array of [price, size], got {type(raw).__name__}")
    levels: list[PriceLevel] = []
    for index, item in enumerate(raw):
        entry_path = f"{path}[{index}]"
        if isinstance(item, (str, bytes)) or not isinstance(item, Sequence) or len(item) != 2:
            raise EventStoreFormatError(f"{entry_path}: expected [price, size]")
        price = _require_number(item[0], path=f"{entry_path}[0]")
        size = _require_number(item[1], path=f"{entry_path}[1]")
        try:
            levels.append(PriceLevel(price=price, size=size))
        except MarketEventError as exc:
            raise EventStoreFormatError(f"{entry_path}: {exc}") from exc
    return tuple(levels)


def _reject_json_constant(name: str) -> object:
    raise EventStoreFormatError(f"non-finite JSON constant is not allowed: {name}")


def _require_object(value: object, *, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise EventStoreFormatError(f"{path}: expected an object, got {type(value).__name__}")
    return value


def _require_field(message: Mapping[str, object], key: str, *, path: str) -> object:
    try:
        return message[key]
    except KeyError:
        raise EventStoreFormatError(f"{path}: missing required field {key!r}") from None


def _require_str(value: object, *, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise EventStoreFormatError(f"{path}: expected a non-empty string, got {type(value).__name__}")
    return value


def _require_optional_str(value: object, *, path: str) -> str | None:
    if value is None:
        return None
    return _require_str(value, path=path)


def _require_int(value: object, *, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EventStoreFormatError(f"{path}: expected an integer, got {type(value).__name__}")
    return value


def _require_optional_int(value: object, *, path: str) -> int | None:
    if value is None:
        return None
    return _require_int(value, path=path)


def _require_number(value: object, *, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EventStoreFormatError(f"{path}: expected a number, got {type(value).__name__}")
    return float(value)


def _require_milliseconds(value: object, *, path: str) -> Milliseconds:
    return _require_int(value, path=path)


def _decode_venue(value: object, *, path: str) -> Venue:
    text = _require_str(value, path=path)
    try:
        return _VENUES_BY_VALUE[text]
    except KeyError:
        raise EventStoreFormatError(f"{path}: unknown venue {text!r}") from None


def _decode_event_type(value: object, *, path: str) -> EventType:
    text = _require_str(value, path=path)
    try:
        return _EVENT_TYPES_BY_VALUE[text]
    except KeyError:
        raise EventStoreFormatError(f"{path}: unknown event type {text!r}") from None
