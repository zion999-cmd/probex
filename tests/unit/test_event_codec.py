"""SC-1：Event Store codec 的逐字段往返与严格解码。"""

from __future__ import annotations

import json
import unittest

from market.events.types import EventType, MarketEvent
from storage.events.codec import (
    compute_event_id,
    loads_record,
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
    EventStoreFormatError,
    UnsupportedSchemaVersionError,
)
from tests.support import (
    BASE_TS,
    depth_diff_event,
    depth_snapshot_event,
    record_for,
    record_line,
    record_raw,
)


def _snapshot_event() -> MarketEvent:
    return depth_snapshot_event(
        100,
        bids=[(0.0024, 10.0), (0.00239, 20.0)],
        asks=[(0.00245, 5.0)],
        exchange_ts=BASE_TS,
        receive_ts=BASE_TS + 1,
        process_ts=BASE_TS + 2,
    )


def _delta_event() -> MarketEvent:
    return depth_diff_event(
        101,
        103,
        bids=[(0.0024, 12.0), (0.00238, 0.0)],
        asks=[(0.00245, 0.0)],
        exchange_ts=BASE_TS + 3,
        receive_ts=BASE_TS + 4,
        process_ts=BASE_TS + 5,
    )


def _record(event: MarketEvent, *, ordinal: int = 0, source: str | None = None) -> EventRecord:
    return EventRecord(
        ordinal=ordinal,
        event_id=compute_event_id(event),
        schema_version=SCHEMA_VERSION,
        event=event,
        source=source,
    )


def _raw(record: EventRecord) -> dict[str, object]:
    """深拷贝为可变 JSON 结构，便于篡改。"""
    return json.loads(dumps_record(record))


class EventCodecRoundTripTest(unittest.TestCase):
    def test_snapshot_round_trip_is_field_identical(self) -> None:
        event = _snapshot_event()
        self.assertEqual(decode_event(encode_event(event)), event)

    def test_delta_round_trip_is_field_identical(self) -> None:
        event = _delta_event()
        self.assertEqual(decode_event(encode_event(event)), event)

    def test_record_round_trip_via_jsonl_line(self) -> None:
        record = _record(_delta_event(), ordinal=7, source="binance-depth-capture")
        decoded = loads_record(dumps_record(record))
        self.assertEqual(decoded, record)
        self.assertEqual(decoded.ordinal, 7)
        self.assertEqual(decoded.source, "binance-depth-capture")
        self.assertEqual(decoded.schema_version, SCHEMA_VERSION)

    def test_record_without_source_round_trip(self) -> None:
        record = _record(_snapshot_event())
        decoded = loads_record(dumps_record(record))
        self.assertEqual(decoded, record)
        self.assertIsNone(decoded.source)
        self.assertNotIn("source", _raw(record))

    def test_all_eight_event_fields_survive(self) -> None:
        event = _delta_event()
        decoded = decode_event(encode_event(event))
        self.assertIs(decoded.venue, event.venue)
        self.assertEqual(decoded.symbol, event.symbol)
        self.assertIs(decoded.event_type, event.event_type)
        self.assertEqual(decoded.exchange_ts, event.exchange_ts)
        self.assertEqual(decoded.receive_ts, event.receive_ts)
        self.assertEqual(decoded.process_ts, event.process_ts)
        self.assertEqual(decoded.sequence, event.sequence)
        self.assertEqual(decoded.payload, event.payload)

    def test_float_values_survive_exactly(self) -> None:
        event = _snapshot_event()
        decoded = decode_event(encode_event(event))
        self.assertEqual([level.price for level in decoded.payload.bids], [0.0024, 0.00239])
        self.assertEqual(repr(decoded.payload.bids[0].size), repr(10.0))

    def test_encoding_is_canonical_and_stable(self) -> None:
        record = _record(_snapshot_event(), ordinal=3, source="x")
        self.assertEqual(dumps_record(record), dumps_record(record))
        self.assertEqual(canonical_json({"b": 1, "a": 2}), '{"a":2,"b":1}')

    def test_event_id_is_content_addressed_and_stable(self) -> None:
        event = _delta_event()
        first = compute_event_id(event)
        self.assertEqual(first, compute_event_id(_delta_event()))
        self.assertTrue(first.startswith("sha256:"))
        other = compute_event_id(_snapshot_event())
        self.assertNotEqual(first, other)

    def test_event_id_does_not_depend_on_ordinal(self) -> None:
        event = _snapshot_event()
        self.assertEqual(_record(event, ordinal=0).event_id, _record(event, ordinal=99).event_id)


class EventCodecStrictDecodingTest(unittest.TestCase):
    def assert_invalid(self, raw: object, expected: type[Exception] = EventStoreFormatError) -> None:
        with self.assertRaises(expected):
            decode_record(raw)

    def test_non_object_payload_rejected(self) -> None:
        self.assert_invalid([1, 2, 3])
        self.assert_invalid("record")
        self.assert_invalid(None)

    def test_missing_record_fields_rejected(self) -> None:
        for field in ("schema_version", "ordinal", "event_id", "event"):
            with self.subTest(field=field):
                raw = _raw(_record(_snapshot_event()))
                del raw[field]
                self.assert_invalid(raw)

    def test_unsupported_schema_version_rejected(self) -> None:
        for version in (0, 2, 99):
            with self.subTest(version=version):
                raw = _raw(_record(_snapshot_event()))
                raw["schema_version"] = version
                self.assert_invalid(raw, UnsupportedSchemaVersionError)

    def test_non_integer_schema_version_rejected(self) -> None:
        raw = _raw(_record(_snapshot_event()))
        raw["schema_version"] = "1"
        self.assert_invalid(raw)

    def test_invalid_ordinal_rejected(self) -> None:
        for ordinal in (-1, True, "0", None, 1.5):
            with self.subTest(ordinal=ordinal):
                raw = _raw(_record(_snapshot_event()))
                raw["ordinal"] = ordinal
                self.assert_invalid(raw)

    def test_tampered_event_id_rejected(self) -> None:
        raw = _raw(_record(_snapshot_event()))
        raw["event_id"] = "sha256:" + "0" * 64
        self.assert_invalid(raw, EventIntegrityError)

    def test_event_content_tampering_breaks_event_id(self) -> None:
        raw = _raw(_record(_delta_event()))
        event = raw["event"]
        assert isinstance(event, dict)
        event["symbol"] = "ETHUSDT"
        self.assert_invalid(raw, EventIntegrityError)

    def test_non_string_event_id_rejected(self) -> None:
        raw = _raw(_record(_snapshot_event()))
        raw["event_id"] = 7
        self.assert_invalid(raw)

    def test_empty_source_rejected(self) -> None:
        raw = _raw(_record(_snapshot_event()))
        raw["source"] = ""
        self.assert_invalid(raw)

    def test_missing_event_fields_rejected(self) -> None:
        for field in ("venue", "symbol", "event_type", "exchange_ts", "receive_ts", "process_ts", "sequence", "payload"):
            with self.subTest(field=field):
                raw = _raw(_record(_delta_event()))
                event = raw["event"]
                assert isinstance(event, dict)
                del event[field]
                self.assert_invalid(raw)

    def test_unknown_venue_rejected(self) -> None:
        raw = _raw(_record(_snapshot_event()))
        raw["event"]["venue"] = "okx"  # type: ignore[index]
        self.assert_invalid(raw)

    def test_unknown_event_type_rejected(self) -> None:
        raw = _raw(_record(_snapshot_event()))
        raw["event"]["event_type"] = "trade"  # type: ignore[index]
        self.assert_invalid(raw)

    def test_event_type_mismatch_with_payload_rejected(self) -> None:
        raw = _raw(_record(_snapshot_event()))
        raw["event"]["event_type"] = EventType.BOOK_DELTA.value  # type: ignore[index]
        self.assert_invalid(raw)

    def test_sequence_mismatch_with_payload_rejected(self) -> None:
        raw = _raw(_record(_delta_event()))
        raw["event"]["sequence"] = 999  # type: ignore[index]
        self.assert_invalid(raw)

    def test_negative_timestamp_rejected(self) -> None:
        raw = _raw(_record(_snapshot_event()))
        raw["event"]["exchange_ts"] = -1  # type: ignore[index]
        self.assert_invalid(raw)

    def test_empty_symbol_rejected(self) -> None:
        raw = _raw(_record(_snapshot_event()))
        raw["event"]["symbol"] = ""  # type: ignore[index]
        self.assert_invalid(raw)

    def test_unknown_payload_kind_rejected(self) -> None:
        raw = _raw(_record(_snapshot_event()))
        raw["event"]["payload"]["kind"] = "trade"  # type: ignore[index]
        self.assert_invalid(raw)

    def test_missing_payload_fields_rejected(self) -> None:
        for field in ("kind", "last_update_id", "bids", "asks"):
            with self.subTest(field=field):
                raw = _raw(_record(_snapshot_event()))
                payload = raw["event"]["payload"]  # type: ignore[index]
                assert isinstance(payload, dict)
                del payload[field]
                self.assert_invalid(raw)

    def test_missing_delta_payload_fields_rejected(self) -> None:
        for field in ("first_update_id", "last_update_id", "bids", "asks"):
            with self.subTest(field=field):
                raw = _raw(_record(_delta_event()))
                payload = raw["event"]["payload"]  # type: ignore[index]
                assert isinstance(payload, dict)
                del payload[field]
                self.assert_invalid(raw)

    def test_bad_level_shapes_rejected(self) -> None:
        for level in ([[1.0]], [[1.0, 2.0, 3.0]], [5], "1.0", [[-1.0, 1.0]], [[1.0, -1.0]], [[0.0, 1.0]]):
            with self.subTest(level=level):
                raw = _raw(_record(_snapshot_event()))
                raw["event"]["payload"]["bids"] = [level]  # type: ignore[index]
                self.assert_invalid(raw)

    def test_levels_must_be_array(self) -> None:
        raw = _raw(_record(_snapshot_event()))
        raw["event"]["payload"]["bids"] = {"0.0024": 10.0}  # type: ignore[index]
        self.assert_invalid(raw)

    def test_snapshot_zero_size_level_rejected(self) -> None:
        raw = _raw(_record(_snapshot_event()))
        raw["event"]["payload"]["bids"] = [[0.0024, 0.0]]  # type: ignore[index]
        self.assert_invalid(raw)

    def test_delta_inverted_update_range_rejected(self) -> None:
        raw = _raw(_record(_delta_event()))
        raw["event"]["payload"]["first_update_id"] = 200  # type: ignore[index]
        self.assert_invalid(raw)

    def test_encode_event_requires_market_event(self) -> None:
        with self.assertRaises(EventStoreFormatError):
            encode_event(object())  # type: ignore[arg-type]

    def test_encode_record_requires_market_event_inside(self) -> None:
        record = EventRecord(ordinal=0, event_id="sha256:x", schema_version=SCHEMA_VERSION, event=object())  # type: ignore[arg-type]
        with self.assertRaises(EventStoreFormatError):
            encode_record(record)


class JsonLineDecodingTest(unittest.TestCase):
    def test_empty_line_rejected(self) -> None:
        with self.assertRaises(EventStoreFormatError):
            loads_record("")
        with self.assertRaises(EventStoreFormatError):
            loads_record("   \n")

    def test_invalid_json_rejected(self) -> None:
        with self.assertRaises(EventStoreFormatError):
            loads_record("{not json\n")
        with self.assertRaises(EventStoreFormatError):
            loads_record("]}\n")

    def test_json_scalar_rejected(self) -> None:
        with self.assertRaises(EventStoreFormatError):
            loads_record("null\n")
        with self.assertRaises(EventStoreFormatError):
            loads_record("[1,2,3]\n")

    def test_non_finite_constants_rejected(self) -> None:
        line = dumps_record(_record(_delta_event()))
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant=constant):
                tampered = line.replace("[0.0024,12.0]", f"[{constant},12.0]")
                self.assertNotEqual(tampered, line)
                with self.assertRaises(EventStoreFormatError):
                    loads_record(tampered)

    def test_non_string_line_rejected(self) -> None:
        with self.assertRaises(EventStoreFormatError):
            loads_record(b"{}")  # type: ignore[arg-type]


class DeltaPreviousUpdateIdCodecTest(unittest.TestCase):
    """P0001.9.1.1 SC-5：`pu` 必须能随 Event Store 往返（否则 replay 会丢失 venue 语义）。"""

    def test_roundtrip_with_previous_update_id(self) -> None:
        event = depth_diff_event(101, 200, bids=[(100.0, 2.0)], previous_update_id=100)
        record = record_for(event)

        restored = loads_record(record_line(record)).event

        self.assertEqual(restored.payload.previous_update_id, 100)
        self.assertEqual(restored.payload.first_update_id, 101)
        self.assertEqual(compute_event_id(restored), compute_event_id(event))

    def test_roundtrip_without_previous_update_id(self) -> None:
        event = depth_diff_event(101, 200, bids=[(100.0, 2.0)])

        restored = loads_record(record_line(record_for(event))).event

        self.assertIsNone(restored.payload.previous_update_id)
        self.assertNotIn("previous_update_id", record_raw(record_for(event))["event"]["payload"])

    def test_encoded_record_carries_the_field_when_present(self) -> None:
        event = depth_diff_event(101, 200, bids=[(100.0, 2.0)], previous_update_id=100)

        payload = record_raw(record_for(event))["event"]["payload"]

        self.assertEqual(payload["previous_update_id"], 100)


if __name__ == "__main__":
    unittest.main()
