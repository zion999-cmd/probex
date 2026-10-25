"""P0017 数据集完整性补强（人类审查 P1，2026-10-24）：

manifest 是**完整性证明**而不只是元数据：
- Reader 读取已定稿分片时必须重新核验 sha256 / bytes / rows / first_ts / last_ts；
- 行内 venue/symbol/kind 必须与路径键一致，schema_version 必须受信，时间戳必须单调；
- Writer 续写已有分片时做同样校验，并核对已有 manifest 条目（stale manifest fail-closed）。

任何篡改 / 不一致都必须抛 MarketDatasetFormatError（fail-closed，不静默接受）。
"""

from __future__ import annotations

import hashlib
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
)

DAY0 = 1704067200000  # 2024-01-01T00:00:00Z
TS_A = DAY0 + 1_000
TS_B = DAY0 + 2_000
TS_C = DAY0 + 3_000


class MarketDatasetIntegrityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="mkt-integrity-"))
        self.kind_dir = self.tmp / "binance" / "BTCUSDT" / "trade"

    def _write_finalized(self) -> pathlib.Path:
        with MarketDatasetWriter(self.tmp, venue="binance", symbol="BTCUSDT",
                                   kind="trade") as w:
            w.append(exchange_ts=TS_A, payload={"price": 100.0})
            w.append(exchange_ts=TS_B, payload={"price": 101.0})
        return self.kind_dir / "2024-01-01" / "data.jsonl"

    def _manifest(self) -> dict[str, object]:
        return json.loads((self.kind_dir / "manifest.json").read_text(encoding="utf-8"))

    def _write_manifest(self, manifest: dict[str, object]) -> None:
        (self.kind_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    def _tampered_manifest_entry(self, **overrides: object) -> dict[str, object]:
        manifest = self._manifest()
        entry = manifest["partitions"]["2024-01-01"]  # type: ignore[index]
        entry.update(overrides)  # type: ignore[union-attr]
        return manifest

    # ---------------------------------------------------------- Reader：内容核验

    def test_reader_rejects_tampered_content_even_when_json_valid(self) -> None:
        path = self._write_finalized()
        lines = path.read_text(encoding="utf-8").splitlines()
        lines[0] = lines[0].replace("100.0", "999.0")  # 改 payload，仍为合法 JSON
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaises(MarketDatasetFormatError):
            MarketDatasetReader(self.tmp, venue="binance", symbol="BTCUSDT",
                                  kind="trade").read()

    def test_reader_rejects_appended_extra_row(self) -> None:
        path = self._write_finalized()
        with path.open("a", encoding="utf-8") as h:
            record = {
                "venue": "binance", "symbol": "BTCUSDT", "kind": "trade",
                "exchange_ts": TS_C, "receive_ts": TS_C, "source": "binance.public",
                "schema_version": MARKET_DATASET_SCHEMA, "payload": {"price": 102.0}}
            h.write(json.dumps(record, ensure_ascii=False) + "\n")
        with self.assertRaises(MarketDatasetFormatError):
            MarketDatasetReader(self.tmp, venue="binance", symbol="BTCUSDT",
                                  kind="trade").read()

    def test_reader_rejects_rows_mismatch(self) -> None:
        self._write_finalized()
        self._write_manifest(self._tampered_manifest_entry(rows=99))
        with self.assertRaises(MarketDatasetFormatError):
            self._reader().read()

    def test_reader_rejects_bytes_mismatch(self) -> None:
        self._write_finalized()
        self._write_manifest(self._tampered_manifest_entry(bytes=1))
        with self.assertRaises(MarketDatasetFormatError):
            self._reader().read()

    def test_reader_rejects_first_ts_mismatch(self) -> None:
        self._write_finalized()
        self._write_manifest(self._tampered_manifest_entry(first_ts=TS_A + 999_999))
        with self.assertRaises(MarketDatasetFormatError):
            self._reader().read()

    def test_reader_rejects_last_ts_mismatch(self) -> None:
        self._write_finalized()
        self._write_manifest(self._tampered_manifest_entry(last_ts=TS_C + 999_999))
        with self.assertRaises(MarketDatasetFormatError):
            self._reader().read()

    def test_reader_rejects_foreign_sha256(self) -> None:
        self._write_finalized()
        self._write_manifest(self._tampered_manifest_entry(sha256="0" * 64))
        with self.assertRaises(MarketDatasetFormatError):
            self._reader().read()

    def test_reader_rejects_malformed_manifest(self) -> None:
        self._write_finalized()
        (self.kind_dir / "manifest.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(MarketDatasetFormatError):
            self._reader().read()

    def test_reader_rejects_manifest_missing_required_field(self) -> None:
        self._write_finalized()
        manifest = self._manifest()
        del manifest["partitions"]["2024-01-01"]["rows"]  # type: ignore[index]
        self._write_manifest(manifest)
        with self.assertRaises(MarketDatasetFormatError):
            self._reader().read()

    def test_reader_rejects_record_key_mismatching_path(self) -> None:
        path = self._write_finalized()
        lines = path.read_text(encoding="utf-8").splitlines()
        row = json.loads(lines[0])
        row["symbol"] = "ETHUSDT"  # 放在 BTCUSDT 目录下却声称 ETHUSDT
        lines[0] = json.dumps(row, ensure_ascii=False)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaises(MarketDatasetFormatError):
            self._reader().read()

    def test_reader_rejects_unknown_schema_version(self) -> None:
        path = self._write_finalized()
        lines = path.read_text(encoding="utf-8").splitlines()
        row = json.loads(lines[0])
        row["schema_version"] = "market-dataset/v9"
        lines[0] = json.dumps(row, ensure_ascii=False)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaises(MarketDatasetFormatError):
            self._reader().read()

    def test_reader_rejects_non_monotonic_existing_rows(self) -> None:
        path = self._write_finalized()
        lines = path.read_text(encoding="utf-8").splitlines()
        row = json.loads(lines[1])
        row["exchange_ts"] = TS_A - 1  # 第二行时间早于第一行
        lines[1] = json.dumps(row, ensure_ascii=False)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaises(MarketDatasetFormatError):
            self._reader().read()

    def test_attested_partition_still_reads_after_verification(self) -> None:
        self._write_finalized()
        records = self._reader().read()
        self.assertEqual([r.exchange_ts for r in records], [TS_A, TS_B])
        raw = (self.kind_dir / "2024-01-01" / "data.jsonl").read_bytes()
        self.assertEqual(
            self._manifest()["partitions"]["2024-01-01"]["sha256"],  # type: ignore[index]
            hashlib.sha256(raw).hexdigest())

    # ---------------------------------------------------------- Writer：续写校验

    def test_writer_reopen_validates_existing_rows_and_reattests(self) -> None:
        self._write_finalized()
        with MarketDatasetWriter(self.tmp, venue="binance", symbol="BTCUSDT",
                                   kind="trade") as w:
            w.append(exchange_ts=TS_C, payload={"price": 102.0})
        records = self._reader().read()
        self.assertEqual([r.exchange_ts for r in records], [TS_A, TS_B, TS_C])
        entry = self._manifest()["partitions"]["2024-01-01"]  # type: ignore[index]
        self.assertEqual(entry["rows"], 3)  # type: ignore[index]
        raw = (self.kind_dir / "2024-01-01" / "data.jsonl").read_bytes()
        self.assertEqual(entry["sha256"], hashlib.sha256(raw).hexdigest())  # type: ignore[index]

    def test_writer_reopen_rejects_foreign_key_row(self) -> None:
        path = self._write_finalized()
        lines = path.read_text(encoding="utf-8").splitlines()
        row = json.loads(lines[0])
        row["venue"] = "other-venue"
        lines[0] = json.dumps(row, ensure_ascii=False)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        # 同步修正 manifest（否则会先报 manifest hash 不符）
        entry = self._manifest()["partitions"]["2024-01-01"]  # type: ignore[index]
        entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()  # type: ignore[index]
        entry["bytes"] = len(path.read_bytes())  # type: ignore[index]
        self._write_manifest(self._manifest())
        with self.assertRaises(MarketDatasetFormatError):
            with MarketDatasetWriter(self.tmp, venue="binance", symbol="BTCUSDT",
                                       kind="trade") as w:
                w.append(exchange_ts=TS_C, payload={"price": 102.0})
        # 校验失败不得追加任何字节
        self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 2)

    def test_writer_reopen_rejects_foreign_schema_row(self) -> None:
        path = self._write_finalized()
        lines = path.read_text(encoding="utf-8").splitlines()
        row = json.loads(lines[0])
        row["schema_version"] = "market-dataset/v9"
        lines[0] = json.dumps(row, ensure_ascii=False)
        new_bytes = ("\n".join(lines) + "\n").encode("utf-8")
        path.write_bytes(new_bytes)
        entry = self._manifest()["partitions"]["2024-01-01"]  # type: ignore[index]
        entry["sha256"] = hashlib.sha256(new_bytes).hexdigest()  # type: ignore[index]
        entry["bytes"] = len(new_bytes)  # type: ignore[index]
        self._write_manifest(self._manifest())
        with self.assertRaises(MarketDatasetFormatError):
            with MarketDatasetWriter(self.tmp, venue="binance", symbol="BTCUSDT",
                                       kind="trade") as w:
                w.append(exchange_ts=TS_C, payload={"price": 102.0})
        self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 2)

    def test_writer_reopen_rejects_non_monotonic_existing_rows(self) -> None:
        path = self._write_finalized()
        lines = path.read_text(encoding="utf-8").splitlines()
        row = json.loads(lines[1])
        row["exchange_ts"] = TS_A - 1
        lines[1] = json.dumps(row, ensure_ascii=False)
        new_bytes = ("\n".join(lines) + "\n").encode("utf-8")
        path.write_bytes(new_bytes)
        entry = self._manifest()["partitions"]["2024-01-01"]  # type: ignore[index]
        entry["sha256"] = hashlib.sha256(new_bytes).hexdigest()  # type: ignore[index]
        entry["bytes"] = len(new_bytes)  # type: ignore[index]
        entry["last_ts"] = TS_A - 1  # type: ignore[index]
        self._write_manifest(self._manifest())
        with self.assertRaises(MarketDatasetFormatError):
            with MarketDatasetWriter(self.tmp, venue="binance", symbol="BTCUSDT",
                                       kind="trade") as w:
                w.append(exchange_ts=TS_C, payload={"price": 102.0})
        self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 2)

    def test_writer_reopen_rejects_stale_manifest_hash(self) -> None:
        path = self._write_finalized()  # manifest 已证明 2 行
        with path.open("a", encoding="utf-8") as h:  # 模拟崩溃后：文件多出未证明内容
            record = {
                "venue": "binance", "symbol": "BTCUSDT", "kind": "trade",
                "exchange_ts": TS_C, "receive_ts": TS_C, "source": "binance.public",
                "schema_version": MARKET_DATASET_SCHEMA, "payload": {"price": 102.0}}
            h.write(json.dumps(record, ensure_ascii=False) + "\n")
        with self.assertRaises(MarketDatasetFormatError):
            with MarketDatasetWriter(self.tmp, venue="binance", symbol="BTCUSDT",
                                       kind="trade") as w:
                w.append(exchange_ts=TS_C + 1, payload={"price": 103.0})
        # 未证明内容存在时不得追加；原文件保持 3 行（未被截断/重写）
        self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 3)

    def test_writer_reopen_without_manifest_entry_still_validates_rows(self) -> None:
        # 未定稿分片（manifest 存在但无该日条目）：续写仍须校验行内容
        path = self._write_finalized()
        manifest = self._manifest()
        del manifest["partitions"]["2024-01-01"]
        self._write_manifest(manifest)
        with MarketDatasetWriter(self.tmp, venue="binance", symbol="BTCUSDT",
                                   kind="trade") as w:
            w.append(exchange_ts=TS_C, payload={"price": 102.0})
        self.assertEqual(len(self._reader().read()), 3)

    def _reader(self) -> MarketDatasetReader:
        return MarketDatasetReader(self.tmp, venue="binance", symbol="BTCUSDT", kind="trade")


if __name__ == "__main__":
    unittest.main()
