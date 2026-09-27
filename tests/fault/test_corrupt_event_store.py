"""SC-6：损坏的 Event Store 必须 fail closed。"""

from __future__ import annotations

import json
import unittest

from market.events.types import MarketEvent
from market.replay.source import ReplayMode, ReplaySource
from storage.events.errors import (
    EventIntegrityError,
    EventOrdinalError,
    EventStoreError,
    EventStoreFormatError,
    UnsupportedSchemaVersionError,
)
from storage.events.reader import JsonlEventReader
from storage.events.writer import JsonlEventWriter
from tests.support import (
    BASE_TS,
    TempDirTestCase,
    depth_diff_event,
    depth_snapshot_event,
    record_for,
    record_line,
    record_raw,
)


def _events() -> list[MarketEvent]:
    return [
        depth_snapshot_event(100, bids=[(100.0, 1.0)], asks=[(100.5, 1.5)], exchange_ts=BASE_TS),
        depth_diff_event(101, 101, bids=[(100.0, 2.0)], exchange_ts=BASE_TS + 1),
        depth_diff_event(102, 102, asks=[(100.5, 0.0)], exchange_ts=BASE_TS + 2),
    ]


class CorruptEventStoreTest(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.events = _events()
        self.lines = [record_line(record_for(event, ordinal=index)) for index, event in enumerate(self.events)]

    def _tampered_lines(self, mutation: str) -> list[str]:
        """按名字对最后一条记录做一次篡改。"""
        raw = record_raw(record_for(self.events[-1], ordinal=2))
        event = raw["event"]
        assert isinstance(event, dict)
        payload = event["payload"]
        assert isinstance(payload, dict)

        if mutation == "drop_record_field:schema_version":
            del raw["schema_version"]
        elif mutation == "schema_version:2":
            raw["schema_version"] = 2
        elif mutation == "drop_record_field:ordinal":
            del raw["ordinal"]
        elif mutation == "ordinal:duplicate":
            raw["ordinal"] = 1
        elif mutation == "event_id:tampered":
            raw["event_id"] = "sha256:" + "0" * 64
        elif mutation == "event:symbol":
            event["symbol"] = "ETHUSDT"
        elif mutation == "event:sequence":
            event["sequence"] = 999
        elif mutation == "event:exchange_ts":
            event["exchange_ts"] = -1
        elif mutation == "payload:kind":
            payload["kind"] = "trade"
        elif mutation == "payload:bids_negative_price":
            payload["bids"] = [[-1.0, 1.0]]
        elif mutation == "drop_event_field:payload":
            del event["payload"]
        else:  # pragma: no cover - 防御性分支
            raise AssertionError(f"unknown mutation {mutation}")

        # 重新写回，且不更新 event_id
        return self.lines[:2] + [json.dumps(raw, sort_keys=True, separators=(",", ":")) + "\n"]

    def _write(self, lines: list[str]) -> None:
        self.store_path().write_text("".join(lines), encoding="utf-8")

    def _assert_fail_closed(self, lines: list[str], expected: type[EventStoreError]) -> None:
        self._write(lines)
        with self.assertRaises(expected):
            JsonlEventReader(self.store_path()).read_all()
        with self.assertRaises(expected):
            JsonlEventWriter(self.store_path())

    def test_invalid_json_rejected(self) -> None:
        self._assert_fail_closed(["not json at all\n"], EventStoreFormatError)

    def test_truncated_line_rejected(self) -> None:
        self._assert_fail_closed([self.lines[0][: len(self.lines[0]) // 2]], EventStoreFormatError)

    def test_blank_line_rejected(self) -> None:
        self._assert_fail_closed([self.lines[0], "\n", self.lines[1]], EventStoreFormatError)

    def test_json_scalar_rejected(self) -> None:
        self._assert_fail_closed(["null\n"], EventStoreFormatError)

    def test_missing_schema_version_rejected(self) -> None:
        self._assert_fail_closed(self._tampered_lines("drop_record_field:schema_version"), EventStoreFormatError)

    def test_unknown_schema_version_rejected(self) -> None:
        self._assert_fail_closed(self._tampered_lines("schema_version:2"), UnsupportedSchemaVersionError)

    def test_missing_ordinal_rejected(self) -> None:
        self._assert_fail_closed(self._tampered_lines("drop_record_field:ordinal"), EventStoreFormatError)

    def test_duplicate_ordinal_rejected(self) -> None:
        self._assert_fail_closed(self._tampered_lines("ordinal:duplicate"), EventOrdinalError)

    def test_tampered_event_id_rejected(self) -> None:
        self._assert_fail_closed(self._tampered_lines("event_id:tampered"), EventIntegrityError)

    def test_tampered_event_content_rejected(self) -> None:
        self._assert_fail_closed(self._tampered_lines("event:symbol"), EventIntegrityError)

    def test_invalid_market_event_rejected(self) -> None:
        self._assert_fail_closed(self._tampered_lines("event:sequence"), EventStoreFormatError)

    def test_negative_timestamp_rejected(self) -> None:
        self._assert_fail_closed(self._tampered_lines("event:exchange_ts"), EventStoreFormatError)

    def test_missing_event_payload_rejected(self) -> None:
        self._assert_fail_closed(self._tampered_lines("drop_event_field:payload"), EventStoreFormatError)

    def test_unknown_payload_kind_rejected(self) -> None:
        self._assert_fail_closed(self._tampered_lines("payload:kind"), EventStoreFormatError)

    def test_negative_price_rejected(self) -> None:
        self._assert_fail_closed(self._tampered_lines("payload:bids_negative_price"), EventStoreFormatError)

    def test_reversed_ordinal_rejected(self) -> None:
        lines = [self.lines[1], self.lines[0]]
        self._assert_fail_closed(lines, EventOrdinalError)

    def test_non_finite_number_rejected(self) -> None:
        tampered = self.lines[1].replace("[[100.0,2.0]]", "[[NaN,2.0]]")
        self.assertNotEqual(tampered, self.lines[1])
        self._assert_fail_closed([self.lines[0], tampered, self.lines[2]], EventStoreFormatError)

    def test_valid_store_is_accepted(self) -> None:
        self._write(self.lines)
        self.assertEqual(len(JsonlEventReader(self.store_path()).read_all()), 3)


class CorruptStoreReplayTest(TempDirTestCase):
    """SC-6：Replay 遇到损坏输入必须报错，不得部分成功。"""

    def setUp(self) -> None:
        super().setUp()
        events = _events()
        self.lines = [record_line(record_for(event, ordinal=index)) for index, event in enumerate(events)]

    def _write_corrupt_tail(self) -> None:
        self.store_path().write_text("".join(self.lines) + "{broken\n", encoding="utf-8")

    def test_full_replay_raises_and_does_not_silently_skip(self) -> None:
        self._write_corrupt_tail()
        source = ReplaySource(JsonlEventReader(self.store_path()))

        consumed = []
        with self.assertRaises(EventStoreFormatError):
            for event in source.iter_events():
                consumed.append(event)

        self.assertEqual(len(consumed), 3, "损坏前的记录可以被消费，但不得跳过损坏记录后继续")

    def test_step_replay_raises_on_corrupt_record(self) -> None:
        self._write_corrupt_tail()
        source = ReplaySource(JsonlEventReader(self.store_path()), mode=ReplayMode.STEP)

        self.assertIsNotNone(source.next_event())
        self.assertIsNotNone(source.next_event())
        self.assertIsNotNone(source.next_event())
        with self.assertRaises(EventStoreFormatError):
            source.next_event()


if __name__ == "__main__":
    unittest.main()
