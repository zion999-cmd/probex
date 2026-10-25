"""回测直接消费 P0017.1 共享市场数据集（D-055 批次1 切片3）。

- 数据集目录（<root>/<venue>/<symbol>/<kind>/<day>/data.jsonl，payload=canonical 事件字典）
  与 event-store 文件同为合法回测输入；
- 跨 kind 合并且确定性排序；路径键与事件内容不符 fail-closed；
- 目录身份只依赖分片内容（不依赖根路径/mtime）。
"""

from __future__ import annotations

import pathlib
import tempfile
import unittest

from backtest import BacktestError, BacktestStatus
from backtest.dataset_source import (
    discover_dataset_symbols,
    is_dataset_dir,
    load_records,
)
from backtest.identity import identity_for_input
from market.events.types import EventType
from storage.events.codec import decode_event, encode_event
from storage.market_dataset import MarketDatasetWriter
from market.events.payloads import AggressorSide
from tests.support import depth_diff_event, depth_snapshot_event, mark_price_event, trade_event

BASE = 1_700_000_000_000  # 2023-11-14，避免跨 UTC 日分片复杂性


def _write_dataset(root: pathlib.Path, records, *, venue: str = "binance",
                    symbol: str = "BTCUSDT") -> None:
    writers: dict[str, MarketDatasetWriter] = {}
    try:
        for record in records:
            event = record.event
            kind = {EventType.BOOK_SNAPSHOT: "book", EventType.BOOK_DELTA: "book",
                    EventType.TRADE: "trade", EventType.MARK_PRICE: "mark"}[event.event_type]
            writer = writers.get(kind)
            if writer is None:
                writer = MarketDatasetWriter(root, venue=venue, symbol=symbol, kind=kind,
                                                source="event-store-import")
                writers[kind] = writer
            writer.append(exchange_ts=int(event.exchange_ts),
                            receive_ts=int(event.receive_ts),
                            payload=encode_event(event))
    finally:
        for writer in writers.values():
            writer.close()


def _event_store_records():
    events = [
        depth_snapshot_event(100, bids=((60_000.0, 1.0),), asks=((60_100.0, 1.0),),
                                exchange_ts=BASE, receive_ts=BASE, process_ts=BASE),
        depth_diff_event(100, 200, previous_update_id=100,
                           bids=((59_999.0, 0.5),), asks=(),
                           exchange_ts=BASE + 100, receive_ts=BASE + 100, process_ts=BASE + 100),
        trade_event(1, price=60_000.5, quantity=0.2, aggressor=AggressorSide.BUY,
                     exchange_ts=BASE + 200, receive_ts=BASE + 200, process_ts=BASE + 200),
        mark_price_event(price=60_000.0, exchange_ts=BASE + 300,
                           receive_ts=BASE + 300, process_ts=BASE + 300),
    ]
    return events


class BacktestDatasetSourceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="bt-dataset-"))

    def _records_from_events(self, events):
        from storage.events.codec import EventRecord, SCHEMA_VERSION, compute_event_id
        return tuple(
            EventRecord(ordinal=i, event_id=compute_event_id(e),
                         schema_version=SCHEMA_VERSION, event=e)
            for i, e in enumerate(events))

    def test_dataset_dir_round_trips_all_events(self) -> None:
        records = self._records_from_events(_event_store_records())
        _write_dataset(self.tmp, records)
        loaded = load_records(self.tmp, symbols=("BTCUSDT",))
        self.assertEqual(len(loaded), len(records))
        self.assertTrue(is_dataset_dir(self.tmp))
        symbols = discover_dataset_symbols(self.tmp)
        self.assertEqual([(s.venue, s.symbol) for s in symbols], [("binance", "BTCUSDT")])
        for loaded_record, original in zip(loaded, records):
            decoded = decode_event(encode_event(loaded_record.event))
            expected = decode_event(encode_event(original.event))
            self.assertEqual(decoded.event_type, expected.event_type)
            self.assertEqual(decoded.exchange_ts, expected.exchange_ts)
            self.assertEqual(decoded.payload, expected.payload)

    def test_records_are_deterministically_ordered_across_kinds(self) -> None:
        # 同一毫秒：snapshot < delta < mark < trade（显式优先级）
        events = [
            trade_event(1, price=60_000.0, quantity=0.1, aggressor=AggressorSide.BUY, exchange_ts=BASE + 5,
                         receive_ts=BASE + 5, process_ts=BASE + 5),
            mark_price_event(price=60_000.0, exchange_ts=BASE + 5,
                               receive_ts=BASE + 5, process_ts=BASE + 5),
            depth_diff_event(100, 200, previous_update_id=100,
                               bids=((60_000.0, 1.0),), asks=(),
                               exchange_ts=BASE + 5, receive_ts=BASE + 5, process_ts=BASE + 5),
            depth_snapshot_event(100, bids=((60_000.0, 1.0),), asks=((60_100.0, 1.0),),
                                    exchange_ts=BASE + 5, receive_ts=BASE + 5, process_ts=BASE + 5),
        ]
        _write_dataset(self.tmp, self._records_from_events(events))
        loaded = load_records(self.tmp)
        self.assertEqual(
            [e.event.event_type for e in loaded],
            [EventType.BOOK_SNAPSHOT, EventType.BOOK_DELTA,
             EventType.MARK_PRICE, EventType.TRADE])
        # 双跑顺序一致
        second = load_records(self.tmp)
        self.assertEqual([encode_event(e.event) for e in loaded],
                          [encode_event(e.event) for e in second])

    def test_missing_declared_symbol_fails_closed(self) -> None:
        records = self._records_from_events(_event_store_records())
        _write_dataset(self.tmp, records)
        with self.assertRaises(BacktestError) as ctx:
            load_records(self.tmp, symbols=("ETHUSDT",))
        self.assertIs(ctx.exception.status, BacktestStatus.INVALID_INPUT)

    def test_event_type_in_wrong_kind_directory_fails_closed(self) -> None:
        records = self._records_from_events(_event_store_records())
        _write_dataset(self.tmp, records)
        # 把一条 trade 事件挪进 book 分片
        trade_path = next((self.tmp / "binance" / "BTCUSDT" / "trade").rglob("data.jsonl"))
        line = trade_path.read_text(encoding="utf-8").splitlines()[0]
        trade_path.unlink()
        # manifest 已证明该文件存在 ⇒ 删除后 book 续写不受影响，直接把 trade 行追加到 book 原始分片
        book_path = next((self.tmp / "binance" / "BTCUSDT" / "book").rglob("data.jsonl"))
        with book_path.open("a", encoding="utf-8") as h:
            h.write(line + "\n")
        with self.assertRaises(BacktestError):
            load_records(self.tmp)

    def test_empty_or_plain_directory_is_invalid(self) -> None:
        with self.assertRaises(BacktestError):
            load_records(self.tmp)

    def test_identity_is_content_only_and_changes_with_rows(self) -> None:
        records = self._records_from_events(_event_store_records())
        _write_dataset(self.tmp, records)
        identity_a = identity_for_input(self.tmp, symbols=("BTCUSDT",))
        import shutil
        other_parent = pathlib.Path(tempfile.mkdtemp(prefix="bt-dataset-move-"))
        moved = other_parent / "dataset"
        shutil.move(str(self.tmp), str(moved))
        identity_b = identity_for_input(moved, symbols=("BTCUSDT",))
        self.assertEqual(identity_a["sha256"], identity_b["sha256"])

        extended_dir = pathlib.Path(tempfile.mkdtemp(prefix="bt-dataset-ext-"))
        extended = records + self._records_from_events(
            [trade_event(2, price=60_001.0, quantity=0.3, aggressor=AggressorSide.SELL,
                           exchange_ts=BASE + 5_000, receive_ts=BASE + 5_000,
                           process_ts=BASE + 5_000)])
        _write_dataset(extended_dir, extended)
        identity_c = identity_for_input(extended_dir, symbols=("BTCUSDT",))
        self.assertNotEqual(identity_a["sha256"], identity_c["sha256"])


if __name__ == "__main__":
    unittest.main()
