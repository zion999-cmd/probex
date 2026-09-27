"""SC-1 / SC-2：Event Store 往返一致性与 append-only 约束。"""

from __future__ import annotations

import unittest

from market.events.types import MarketEvent
from storage.events.codec import SCHEMA_VERSION, EventRecord, compute_event_id, dumps_record
from storage.events.errors import EventOrdinalError, EventStoreError, EventStoreFormatError
from storage.events.reader import JsonlEventReader
from storage.events.writer import JsonlEventWriter
from tests.support import BASE_TS, TempDirTestCase, depth_diff_event, depth_snapshot_event, write_store


def _events() -> list[MarketEvent]:
    return [
        depth_snapshot_event(100, bids=[(100.0, 1.0)], asks=[(100.5, 1.5)], exchange_ts=BASE_TS),
        depth_diff_event(101, 101, bids=[(100.0, 2.0)], exchange_ts=BASE_TS + 1),
        depth_diff_event(102, 102, asks=[(100.5, 0.0)], exchange_ts=BASE_TS + 2),
        depth_diff_event(103, 103, bids=[(99.0, 5.0)], exchange_ts=BASE_TS + 3),
        depth_diff_event(104, 104, bids=[(99.0, 0.0)], exchange_ts=BASE_TS + 4),
    ]


def _line(event: MarketEvent, *, ordinal: int) -> str:
    record = EventRecord(
        ordinal=ordinal,
        event_id=compute_event_id(event),
        schema_version=SCHEMA_VERSION,
        event=event,
    )
    return dumps_record(record) + "\n"


class EventStoreRoundTripTest(TempDirTestCase):
    """SC-1 验收：写入后读回逐字段一致。"""

    def test_sc1_write_then_read_is_field_identical(self) -> None:
        events = _events()
        path = self.store_path()

        written = write_store(path, events, source="binance-depth-capture")
        read = JsonlEventReader(path).read_all()

        self.assertEqual(len(read), len(events))
        self.assertEqual(read, written)
        self.assertEqual([record.event for record in read], events)
        self.assertEqual([record.ordinal for record in read], [0, 1, 2, 3, 4])
        self.assertTrue(all(record.source == "binance-depth-capture" for record in read))
        self.assertTrue(all(record.schema_version == SCHEMA_VERSION for record in read))
        self.assertEqual(path.read_text(encoding="utf-8").count("\n"), len(events))

    def test_read_all_matches_iteration(self) -> None:
        path = self.store_path()
        write_store(path, _events())
        reader = JsonlEventReader(path)
        self.assertEqual(reader.read_all(), tuple(reader))
        self.assertEqual([record.ordinal for record in reader], [0, 1, 2, 3, 4])

    def test_reader_is_reiterable(self) -> None:
        path = self.store_path()
        write_store(path, _events())
        reader = JsonlEventReader(path)
        self.assertEqual(list(reader), list(reader))


class EventStoreAppendOnlyTest(TempDirTestCase):
    """SC-2 验收：只追加，不覆盖已有 ordinal。"""

    def test_sc2_reopening_writer_continues_ordinals_and_keeps_history(self) -> None:
        events = _events()
        path = self.store_path()

        first = write_store(path, events[:2])
        history = path.read_bytes()
        second = write_store(path, events[2:])
        after = path.read_bytes()

        self.assertEqual([record.ordinal for record in first], [0, 1])
        self.assertEqual([record.ordinal for record in second], [2, 3, 4])
        self.assertTrue(after.startswith(history), "已有记录必须字节级不变")
        self.assertEqual([record.event for record in JsonlEventReader(path).read_all()], events)

    def test_next_ordinal_tracks_appends(self) -> None:
        path = self.store_path()
        writer = JsonlEventWriter(path)
        try:
            self.assertEqual(writer.next_ordinal, 0)
            writer.append(_events()[0])
            self.assertEqual(writer.next_ordinal, 1)
            writer.append(_events()[1])
            self.assertEqual(writer.next_ordinal, 2)
        finally:
            writer.close()

    def test_writer_uses_provided_path_for_new_file(self) -> None:
        path = self.store_path("nested.jsonl")
        records = write_store(path, _events()[:1])
        self.assertTrue(path.exists())
        self.assertEqual(records[0].ordinal, 0)

    def test_append_after_close_is_rejected(self) -> None:
        path = self.store_path()
        writer = JsonlEventWriter(path)
        writer.close()
        writer.close()  # 幂等
        with self.assertRaises(EventStoreError):
            writer.append(_events()[0])

    def test_empty_source_rejected(self) -> None:
        path = self.store_path()
        writer = JsonlEventWriter(path)
        try:
            with self.assertRaises(EventStoreError):
                writer.append(_events()[0], source="")
        finally:
            writer.close()

    def test_writer_refuses_to_open_corrupt_store(self) -> None:
        path = self.store_path()
        path.write_text("not json at all\n", encoding="utf-8")
        with self.assertRaises(EventStoreFormatError):
            JsonlEventWriter(path)

    def test_writer_continues_after_empty_file(self) -> None:
        path = self.store_path()
        path.write_text("", encoding="utf-8")
        records = write_store(path, _events()[:1])
        self.assertEqual(records[0].ordinal, 0)

    def test_context_manager_closes_writer(self) -> None:
        path = self.store_path()
        with JsonlEventWriter(path) as writer:
            writer.append(_events()[0])
        self.assertEqual(len(JsonlEventReader(path).read_all()), 1)


class EventStoreOrderingTest(TempDirTestCase):
    def test_duplicate_ordinal_rejected(self) -> None:
        path = self.store_path()
        events = _events()
        path.write_text(_line(events[0], ordinal=0) + _line(events[1], ordinal=0), encoding="utf-8")

        with self.assertRaises(EventOrdinalError):
            JsonlEventReader(path).read_all()

    def test_reversed_ordinal_rejected(self) -> None:
        path = self.store_path()
        events = _events()
        path.write_text(_line(events[0], ordinal=5) + _line(events[1], ordinal=4), encoding="utf-8")

        with self.assertRaises(EventOrdinalError):
            JsonlEventReader(path).read_all()

    def test_gap_in_ordinals_is_allowed(self) -> None:
        # 只要求严格递增；文件切片 / 多段 store 不因缺号而失败
        path = self.store_path()
        events = _events()
        path.write_text(_line(events[0], ordinal=10) + _line(events[1], ordinal=11), encoding="utf-8")

        self.assertEqual([record.ordinal for record in JsonlEventReader(path)], [10, 11])


if __name__ == "__main__":
    unittest.main()
