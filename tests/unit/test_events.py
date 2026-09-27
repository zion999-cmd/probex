"""MarketEvent 与载荷的不变量测试。"""

from __future__ import annotations

import unittest

from market.events.errors import InvalidEventError, InvalidPayloadError
from market.events.payloads import BookDeltaPayload, BookSnapshotPayload, PriceLevel
from market.events.types import EventType, MarketEvent, Venue, event_type_for


def _snapshot(**overrides: object) -> BookSnapshotPayload:
    values: dict[str, object] = {
        "last_update_id": 10,
        "bids": (PriceLevel(price=100.0, size=1.0),),
        "asks": (PriceLevel(price=101.0, size=2.0),),
    }
    values.update(overrides)
    return BookSnapshotPayload(**values)  # type: ignore[arg-type]


def _delta(**overrides: object) -> BookDeltaPayload:
    values: dict[str, object] = {
        "first_update_id": 11,
        "last_update_id": 11,
        "bids": (PriceLevel(price=100.0, size=3.0),),
        "asks": (),
    }
    values.update(overrides)
    return BookDeltaPayload(**values)  # type: ignore[arg-type]


def _event(**overrides: object) -> MarketEvent:
    values: dict[str, object] = {
        "venue": Venue.BINANCE,
        "symbol": "BTCUSDT",
        "event_type": EventType.BOOK_SNAPSHOT,
        "exchange_ts": 1_700_000_000_000,
        "receive_ts": 1_700_000_000_001,
        "process_ts": 1_700_000_000_002,
        "sequence": 10,
        "payload": _snapshot(),
    }
    values.update(overrides)
    return MarketEvent(**values)  # type: ignore[arg-type]


class PayloadInvariantTest(unittest.TestCase):
    def test_snapshot_constructs_with_all_fields(self) -> None:
        payload = _snapshot()
        self.assertEqual(payload.last_update_id, 10)
        self.assertEqual(payload.bids[0], PriceLevel(price=100.0, size=1.0))
        self.assertEqual(len(payload.asks), 1)

    def test_delta_constructs_with_all_fields(self) -> None:
        payload = _delta()
        self.assertEqual((payload.first_update_id, payload.last_update_id), (11, 11))

    def test_price_level_rejects_non_positive_price(self) -> None:
        with self.assertRaises(InvalidPayloadError):
            PriceLevel(price=0.0, size=1.0)

    def test_price_level_rejects_negative_size(self) -> None:
        with self.assertRaises(InvalidPayloadError):
            PriceLevel(price=100.0, size=-1.0)

    def test_price_level_rejects_non_numeric(self) -> None:
        with self.assertRaises(InvalidPayloadError):
            PriceLevel(price="100.0", size=1.0)  # type: ignore[arg-type]

    def test_snapshot_rejects_zero_size_level(self) -> None:
        with self.assertRaises(InvalidPayloadError):
            _snapshot(bids=(PriceLevel(price=100.0, size=0.0),))

    def test_delta_allows_zero_size_level(self) -> None:
        payload = _delta(bids=(PriceLevel(price=100.0, size=0.0),))
        self.assertEqual(payload.bids[0].size, 0.0)

    def test_snapshot_rejects_duplicate_price(self) -> None:
        with self.assertRaises(InvalidPayloadError):
            _snapshot(bids=(PriceLevel(price=100.0, size=1.0), PriceLevel(price=100.0, size=2.0)))

    def test_delta_rejects_inverted_update_range(self) -> None:
        with self.assertRaises(InvalidPayloadError):
            _delta(first_update_id=12, last_update_id=11)

    def test_snapshot_rejects_negative_last_update_id(self) -> None:
        with self.assertRaises(InvalidPayloadError):
            _snapshot(last_update_id=-1)

    def test_snapshot_rejects_plain_levels(self) -> None:
        with self.assertRaises(InvalidPayloadError):
            _snapshot(bids=((100.0, 1.0),))


class MarketEventInvariantTest(unittest.TestCase):
    def test_valid_snapshot_event_keeps_eight_fields(self) -> None:
        event = _event()
        self.assertIs(event.venue, Venue.BINANCE)
        self.assertEqual(event.symbol, "BTCUSDT")
        self.assertIs(event.event_type, EventType.BOOK_SNAPSHOT)
        self.assertEqual(event.exchange_ts, 1_700_000_000_000)
        self.assertEqual(event.receive_ts, 1_700_000_000_001)
        self.assertEqual(event.process_ts, 1_700_000_000_002)
        self.assertEqual(event.sequence, 10)
        self.assertIsInstance(event.payload, BookSnapshotPayload)

    def test_valid_delta_event(self) -> None:
        event = _event(event_type=EventType.BOOK_DELTA, sequence=11, payload=_delta())
        self.assertIs(event.event_type, EventType.BOOK_DELTA)

    def test_event_type_must_match_payload(self) -> None:
        with self.assertRaises(InvalidEventError):
            _event(event_type=EventType.BOOK_DELTA, sequence=11)

    def test_sequence_must_match_payload_update_id(self) -> None:
        with self.assertRaises(InvalidEventError):
            _event(sequence=99)

    def test_sequence_is_required_for_book_events(self) -> None:
        with self.assertRaises(InvalidEventError):
            _event(sequence=None)

    def test_negative_timestamp_rejected(self) -> None:
        with self.assertRaises(InvalidEventError):
            _event(receive_ts=-1)

    def test_non_integer_timestamp_rejected(self) -> None:
        with self.assertRaises(InvalidEventError):
            _event(process_ts=1.5)  # type: ignore[arg-type]

    def test_empty_symbol_rejected(self) -> None:
        with self.assertRaises(InvalidEventError):
            _event(symbol="")

    def test_venue_must_be_venue_enum(self) -> None:
        with self.assertRaises(InvalidEventError):
            _event(venue="binance")

    def test_event_type_must_be_event_type_enum(self) -> None:
        with self.assertRaises(InvalidEventError):
            _event(event_type="book_snapshot", sequence=10)

    def test_event_type_for_rejects_unknown_payload(self) -> None:
        with self.assertRaises(InvalidEventError):
            event_type_for(object())  # type: ignore[arg-type]

    def test_event_type_for_maps_book_payloads(self) -> None:
        self.assertIs(event_type_for(_snapshot()), EventType.BOOK_SNAPSHOT)
        self.assertIs(event_type_for(_delta()), EventType.BOOK_DELTA)


if __name__ == "__main__":
    unittest.main()
