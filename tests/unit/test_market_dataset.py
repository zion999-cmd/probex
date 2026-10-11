"""P0017.1 Batch 2：与 run 解耦的共享市场数据集。"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from storage.market_dataset import (
    MARKET_DATASET_SCHEMA,
    MarketDatasetError,
    MarketDatasetFormatError,
    MarketDatasetReader,
    MarketDatasetWriter,
    utc_day,
)

# 2024-01-01T00:00:00Z 与同日稍后的毫秒时间戳
DAY0 = 1704067200000
TS_A = DAY0 + 1_000
TS_B = DAY0 + 2_000
DAY1 = DAY0 + 24 * 3600 * 1000


class MarketDatasetWriteReadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="mkt-dataset-"))

    def writer(self, kind="trade"):
        return MarketDatasetWriter(self.tmp, venue="binance", symbol="BTCUSDT",
                                    kind=kind, source="binance.public")

    def reader(self, kind="trade"):
        return MarketDatasetReader(self.tmp, venue="binance", symbol="BTCUSDT", kind=kind)

    def test_append_and_read_back(self) -> None:
        with self.writer() as w:
            w.append(exchange_ts=TS_A, payload={"price": 100.0})
            w.append(exchange_ts=TS_B, payload={"price": 101.0})
        records = self.reader().read()
        self.assertEqual([r.exchange_ts for r in records], [TS_A, TS_B])
        self.assertEqual(records[0].schema_version, MARKET_DATASET_SCHEMA)

    def test_out_of_order_within_partition_rejected(self) -> None:
        with self.assertRaises(MarketDatasetError):
            with self.writer() as w:
                w.append(exchange_ts=TS_B, payload={})
                w.append(exchange_ts=TS_A, payload={})

    def test_day_rollover_finalizes_manifest_with_hash(self) -> None:
        with self.writer() as w:
            w.append(exchange_ts=TS_A, payload={})
            w.append(exchange_ts=DAY1, payload={})
        parts = self.reader().partitions()
        self.assertEqual([p.day for p in parts], [utc_day(TS_A), utc_day(DAY1)])
        self.assertTrue(all(p.sha256 and p.rows >= 1 for p in parts))

    def test_reopen_appends_without_duplicate_or_overwrite(self) -> None:
        with self.writer() as w:
            w.append(exchange_ts=TS_A, payload={"v": 1})
        with self.writer() as w:
            w.append(exchange_ts=TS_B, payload={"v": 2})
        records = self.reader().read()
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].payload["v"], 1)

    def test_corrupt_partition_fails_closed(self) -> None:
        with self.writer() as w:
            w.append(exchange_ts=TS_A, payload={})
        data_file = list((self.tmp / "binance/BTCUSDT/trade").rglob("data.jsonl"))[0]
        with data_file.open("a", encoding="utf-8") as h:
            h.write("{not json\n")
        with self.assertRaises(MarketDatasetFormatError):
            self.reader().read()

    def test_range_and_limit(self) -> None:
        with self.writer() as w:
            for i in range(5):
                w.append(exchange_ts=TS_A + i * 100, payload={"i": i})
        self.assertEqual(len(self.reader().read(start_ts=TS_A + 100, end_ts=TS_A + 300)), 3)
        self.assertEqual(len(self.reader().read(limit=2)), 2)

    def test_gap_days_reported_not_interpolated(self) -> None:
        with self.writer() as w:
            w.append(exchange_ts=TS_A, payload={})
            w.append(exchange_ts=DAY1 + 3 * 24 * 3600 * 1000, payload={})
        gaps = self.reader().gaps()
        self.assertTrue(gaps)

    def test_missing_partition_listed_in_manifest_is_fatal(self) -> None:
        with self.writer() as w:
            w.append(exchange_ts=TS_A, payload={})
        data_file = list((self.tmp / "binance/BTCUSDT/trade").rglob("data.jsonl"))[0]
        data_file.unlink()
        with self.assertRaises(MarketDatasetFormatError):
            self.reader().read()

    def test_invalid_key_and_kind_rejected(self) -> None:
        with self.assertRaises(MarketDatasetError):
            MarketDatasetWriter(self.tmp, venue="a/b", symbol="X", kind="trade")
        with self.assertRaises(MarketDatasetError):
            MarketDatasetWriter(self.tmp, venue="binance", symbol="X", kind="bogus")

    def test_symbols_are_isolated(self) -> None:
        for symbol in ("BTCUSDT", "ETHUSDT"):
            with MarketDatasetWriter(self.tmp, venue="binance", symbol=symbol,
                                      kind="trade") as w:
                w.append(exchange_ts=TS_A, payload={"symbol": symbol})
        self.assertEqual(self.reader("trade").read()[0].symbol, "BTCUSDT")
        eth = MarketDatasetReader(self.tmp, venue="binance", symbol="ETHUSDT",
                                   kind="trade").read()
        self.assertEqual(eth[0].payload["symbol"], "ETHUSDT")


if __name__ == "__main__":
    unittest.main()
