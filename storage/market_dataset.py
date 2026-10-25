"""与 run 解耦的共享市场数据集（P0017.1 §2.2）。

布局
----
```
<root>/<venue>/<symbol>/<kind>/<yyyy-mm-dd>/<partition>.jsonl
<root>/<venue>/<symbol>/<kind>/manifest.json
```

- 记录：append-only JSONL，每行一条规范化市场记录
  `{venue,symbol,kind,exchange_ts,receive_ts,source,schema_version,payload,quality?}`。
- **按 exchange 时间戳所属 UTC 日分片**；同一分片只追加。
- 损坏分片 fail-closed（不静默跳过、不插值）。
- manifest 记录每个分片的行数 / 首尾 ts / 字节数 / 内容 hash / schema；
  **已定稿分片不改写**（迟到数据不回改历史，保证回测可复现）。
- **manifest 是完整性证明，不只是元数据**：Reader 读取已定稿分片时重新核验
  sha256 / bytes / rows / first_ts / last_ts，且逐行核验 venue/symbol/kind 与
  schema_version、时间单调性；Writer 续写已有分片前同样核验并对照已有 manifest
  条目（stale attestation fail-closed）。

数据集**不拥有运行状态**：采集进程可独立写入；run 只引用数据集区间/版本（引用留待 P0017.2/.3）。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import TextIO

MARKET_DATASET_SCHEMA = "market-dataset/v1"
VALID_KINDS = ("book", "trade", "mark", "bar")


class MarketDatasetError(Exception):
    """市场数据集错误基类。"""


class MarketDatasetFormatError(MarketDatasetError):
    """分片内容损坏或记录非法。"""


def utc_day(exchange_ts_ms: int) -> str:
    """exchange 毫秒时间戳 → UTC 日 `YYYY-MM-DD`。"""
    return _dt.datetime.fromtimestamp(exchange_ts_ms / 1000, tz=_dt.timezone.utc).strftime("%Y-%m-%d")


def _canonical(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _validate_key(venue: str, symbol: str, kind: str) -> None:
    if not isinstance(venue, str) or not venue or "/" in venue:
        raise MarketDatasetError("venue must be a non-empty path-safe string")
    if not isinstance(symbol, str) or not symbol or "/" in symbol:
        raise MarketDatasetError("symbol must be a non-empty path-safe string")
    if kind not in VALID_KINDS:
        raise MarketDatasetError(f"kind must be one of {VALID_KINDS}, got {kind!r}")


@dataclass(frozen=True, slots=True)
class MarketRecord:
    venue: str
    symbol: str
    kind: str
    exchange_ts: int
    receive_ts: int
    source: str
    payload: object
    schema_version: str = MARKET_DATASET_SCHEMA
    quality: dict[str, object] | None = None
    #: 采集顺序（同一 symbol 跨 kind 的全局到达序号；用于回测在同毫秒跨 kind 时复现真实顺序）。
    #: None = 旧记录/未跟踪，回测退回显式类型优先级。
    ordinal: int | None = None

    def to_line(self) -> str:
        row = {
            "venue": self.venue,
            "symbol": self.symbol,
            "kind": self.kind,
            "exchange_ts": self.exchange_ts,
            "receive_ts": self.receive_ts,
            "source": self.source,
            "schema_version": self.schema_version,
            "payload": self.payload,
        }
        if self.quality is not None:
            row["quality"] = self.quality
        if self.ordinal is not None:
            row["ordinal"] = self.ordinal
        return _canonical(row)

    @staticmethod
    def from_row(row: object, *, path: str) -> "MarketRecord":
        if not isinstance(row, dict):
            raise MarketDatasetFormatError(f"{path}: record must be a JSON object")
        try:
            raw_ordinal = row.get("ordinal")
            ordinal = None if raw_ordinal is None else int(raw_ordinal)
            return MarketRecord(
                venue=row["venue"], symbol=row["symbol"], kind=row["kind"],
                exchange_ts=int(row["exchange_ts"]), receive_ts=int(row["receive_ts"]),
                source=str(row["source"]), payload=row.get("payload"),
                schema_version=str(row.get("schema_version", "")),
                quality=row.get("quality") if isinstance(row.get("quality"), dict) else None,
                ordinal=ordinal)
        except (KeyError, TypeError, ValueError) as exc:
            raise MarketDatasetFormatError(f"{path}: invalid record: {exc}") from None


class MarketDatasetWriter:
    """单个 `(venue,symbol,kind)` 的 append-only 写器（按日自动切分）。"""

    def __init__(self, root: str | Path, *, venue: str, symbol: str, kind: str,
                 source: str = "binance.public") -> None:
        _validate_key(venue, symbol, kind)
        if not source:
            raise MarketDatasetError("source must be non-empty")
        self._root = Path(root)
        self._venue, self._symbol, self._kind, self._source = venue, symbol, kind, source
        self._dir = self._root / venue / symbol / kind
        self._handle: TextIO | None = None
        self._current_day: str | None = None
        self._current_path: Path | None = None
        self._count_in_partition = 0
        self._byte_in_partition = 0
        self._first_ts: int | None = None
        self._last_ts: int | None = None
        self._last_ordinal: int | None = None

    @property
    def current_partition(self) -> str | None:
        return self._current_day

    def append(self, *, exchange_ts: int, receive_ts: int | None = None,
               payload: object = None, quality: dict[str, object] | None = None,
               ordinal: int | None = None) -> MarketRecord:
        if not isinstance(exchange_ts, int) or exchange_ts <= 0:
            raise MarketDatasetError("exchange_ts must be a positive int (ms)")
        if ordinal is not None and (not isinstance(ordinal, int) or ordinal < 0):
            raise MarketDatasetError("ordinal must be a non-negative int")
        day = utc_day(exchange_ts)
        if self._handle is not None and day != self._current_day:
            self._finalize_partition()
        if self._handle is None:
            self._open_partition(day, exchange_ts)
        # 单调校验：同一分片不允许时间戳回退（防乱序悄悄改写历史顺序）
        if self._last_ts is not None and exchange_ts < self._last_ts:
            raise MarketDatasetError(
                f"out-of-order record for {self._symbol}/{self._kind}: {exchange_ts} < {self._last_ts}")
        if ordinal is not None and self._last_ordinal is not None \
                and ordinal <= self._last_ordinal:
            raise MarketDatasetError(
                f"out-of-order ordinal for {self._symbol}/{self._kind}: "
                f"{ordinal} <= {self._last_ordinal}")
        record = MarketRecord(
            venue=self._venue, symbol=self._symbol, kind=self._kind,
            exchange_ts=exchange_ts, receive_ts=receive_ts or exchange_ts,
            source=self._source, payload=payload, quality=quality, ordinal=ordinal)
        if ordinal is not None:
            self._last_ordinal = ordinal
        line = record.to_line() + "\n"
        self._handle.write(line)
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._count_in_partition += 1
        self._byte_in_partition += len(line.encode("utf-8"))
        self._last_ts = exchange_ts
        if self._first_ts is None:
            self._first_ts = exchange_ts
        return record

    def _open_partition(self, day: str, first_ts: int) -> None:
        day_dir = self._dir / day
        day_dir.mkdir(parents=True, exist_ok=True)
        path = day_dir / "data.jsonl"
        if path.exists():
            # 续写：逐行校验已有分片（键 / schema / 单调性），并核对已有 manifest 证明，
            # 然后恢复计数（任何损坏或 stale attestation 都 fail-closed）。
            scan = _scan_partition(
                path, key=(self._venue, self._symbol, self._kind), collect=False)
            manifest = _load_manifest(self._dir)
            if manifest is not None:
                entry = (manifest.get("partitions") or {}).get(day)
                if entry is not None:
                    _verify_attestation(entry, scan, path=path, day=day)
            self._count_in_partition = scan.rows
            self._byte_in_partition = scan.bytes_
            self._first_ts = scan.first_ts
            self._last_ts = scan.last_ts
            self._last_ordinal = scan.last_ordinal
        else:
            self._count_in_partition = 0
            self._byte_in_partition = 0
            self._first_ts = first_ts
            self._last_ts = None
        self._handle = path.open("a", encoding="utf-8", newline="\n")
        self._current_day = day
        self._current_path = path

    def _finalize_partition(self) -> None:
        if self._handle is None or self._current_path is None or self._current_day is None:
            return
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._handle.close()
        _write_manifest_entry(
            kind_dir=self._dir, day=self._current_day, path=self._current_path,
            count=self._count_in_partition, first_ts=self._first_ts, last_ts=self._last_ts)
        self._handle = None
        self._current_day = None
        self._current_path = None
        self._count_in_partition = 0
        self._byte_in_partition = 0
        self._first_ts = self._last_ts = None
        self._last_ordinal = None

    def close(self) -> None:
        self._finalize_partition()

    def __enter__(self) -> "MarketDatasetWriter":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: TracebackType) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class PartitionInfo:
    day: str
    rows: int
    first_ts: int | None
    last_ts: int | None
    bytes_: int
    sha256: str


@dataclass(frozen=True, slots=True)
class _PartitionScan:
    """逐行扫描分片后的事实（完整性校验的唯一计算来源）。"""

    rows: int
    first_ts: int | None
    last_ts: int | None
    bytes_: int
    sha256: str
    records: tuple[MarketRecord, ...]
    last_ordinal: int | None = None


def _scan_partition(path: Path, *, key: tuple[str, str, str],
                    collect: bool) -> _PartitionScan:
    """读取并逐行校验一个分片文件（fail-closed）。

    校验：UTF-8 可读、每行合法 JSON 对象、记录形状（MarketRecord.from_row）、
    venue/symbol/kind 与路径键一致、schema_version 受信、exchange_ts 非递减。
    """
    venue, symbol, kind = key
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MarketDatasetFormatError(f"{path}: partition is not valid UTF-8: {exc}") from None
    except OSError as exc:
        raise MarketDatasetFormatError(f"{path}: cannot read partition: {exc}") from None
    rows = 0
    first_ts: int | None = None
    last_ts: int | None = None
    last_ordinal: int | None = None
    records: list[MarketRecord] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MarketDatasetFormatError(f"{path}:{number}: corrupt JSON: {exc}") from None
        record = MarketRecord.from_row(row, path=f"{path}:{number}")
        if (record.venue, record.symbol, record.kind) != (venue, symbol, kind):
            raise MarketDatasetFormatError(
                f"{path}:{number}: record key {(record.venue, record.symbol, record.kind)!r} "
                f"does not match partition key {(venue, symbol, kind)!r}")
        if record.schema_version != MARKET_DATASET_SCHEMA:
            raise MarketDatasetFormatError(
                f"{path}:{number}: unsupported schema_version {record.schema_version!r} "
                f"(expected {MARKET_DATASET_SCHEMA!r})")
        if last_ts is not None and record.exchange_ts < last_ts:
            raise MarketDatasetFormatError(
                f"{path}:{number}: non-monotonic exchange_ts "
                f"{record.exchange_ts} < {last_ts}")
        if record.ordinal is not None:
            if last_ordinal is not None and record.ordinal <= last_ordinal:
                raise MarketDatasetFormatError(
                    f"{path}:{number}: non-monotonic ordinal "
                    f"{record.ordinal} <= {last_ordinal}")
            last_ordinal = record.ordinal
        rows += 1
        last_ts = record.exchange_ts
        if first_ts is None:
            first_ts = record.exchange_ts
        if collect:
            records.append(record)
    return _PartitionScan(
        rows=rows, first_ts=first_ts, last_ts=last_ts, bytes_=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(), records=tuple(records),
        last_ordinal=last_ordinal)


def _load_manifest(kind_dir: Path) -> dict[str, object] | None:
    """读取并严格校验 manifest；不存在返回 None（未定稿数据集）。"""
    mpath = _manifest_path(kind_dir)
    if not mpath.exists():
        return None
    try:
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MarketDatasetFormatError(f"{mpath}: corrupt manifest JSON: {exc}") from None
    if not isinstance(manifest, dict):
        raise MarketDatasetFormatError(f"{mpath}: manifest must be a JSON object")
    schema = manifest.get("schema")
    if schema != MARKET_DATASET_SCHEMA:
        raise MarketDatasetFormatError(
            f"{mpath}: unsupported manifest schema {schema!r} "
            f"(expected {MARKET_DATASET_SCHEMA!r})")
    partitions = manifest.get("partitions")
    if not isinstance(partitions, dict):
        raise MarketDatasetFormatError(f"{mpath}: manifest.partitions must be an object")
    for day, entry in partitions.items():
        try:
            _dt.date.fromisoformat(day)
        except ValueError:
            raise MarketDatasetFormatError(f"{mpath}: invalid partition day {day!r}") from None
        if not isinstance(entry, dict):
            raise MarketDatasetFormatError(f"{mpath}: partitions[{day!r}] must be an object")
        if entry.get("file") != f"{day}/data.jsonl":
            raise MarketDatasetFormatError(
                f"{mpath}: partitions[{day!r}].file must equal {day}/data.jsonl")
        if not isinstance(entry.get("rows"), int) or entry["rows"] < 0:
            raise MarketDatasetFormatError(f"{mpath}: partitions[{day!r}].rows invalid")
        if not isinstance(entry.get("bytes"), int) or entry["bytes"] < 0:
            raise MarketDatasetFormatError(f"{mpath}: partitions[{day!r}].bytes invalid")
        if not isinstance(entry.get("sha256"), str) or not entry["sha256"]:
            raise MarketDatasetFormatError(f"{mpath}: partitions[{day!r}].sha256 invalid")
        for ts_field in ("first_ts", "last_ts"):
            value = entry.get(ts_field)
            if value is not None and not isinstance(value, int):
                raise MarketDatasetFormatError(
                    f"{mpath}: partitions[{day!r}].{ts_field} must be int or null")
    return manifest


def _verify_attestation(entry: object, scan: _PartitionScan, *, path: Path, day: str) -> None:
    """manifest 条目必须与磁盘分片逐字段一致（完整性证明）。"""
    assert isinstance(entry, dict)
    mismatches: list[str] = []
    if int(entry["rows"]) != scan.rows:
        mismatches.append(f"rows manifest={entry['rows']} actual={scan.rows}")
    if int(entry["bytes"]) != scan.bytes_:
        mismatches.append(f"bytes manifest={entry['bytes']} actual={scan.bytes_}")
    if str(entry["sha256"]) != scan.sha256:
        mismatches.append("sha256 mismatch")
    if entry.get("first_ts") != scan.first_ts:
        mismatches.append(f"first_ts manifest={entry.get('first_ts')} actual={scan.first_ts}")
    if entry.get("last_ts") != scan.last_ts:
        mismatches.append(f"last_ts manifest={entry.get('last_ts')} actual={scan.last_ts}")
    if mismatches:
        raise MarketDatasetFormatError(
            f"manifest attestation mismatch for {day} ({path}): " + "; ".join(mismatches))


def _manifest_path(kind_dir: Path) -> Path:
    return kind_dir / "manifest.json"


def _write_manifest_entry(*, kind_dir: Path, day: str, path: Path, count: int,
                           first_ts: int | None, last_ts: int | None) -> None:
    """原子更新 manifest（temp → fsync → replace → fsync(dir)）。"""
    kind_dir.mkdir(parents=True, exist_ok=True)
    mpath = _manifest_path(kind_dir)
    manifest = _load_manifest(kind_dir) or {}
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest.setdefault("schema", MARKET_DATASET_SCHEMA)
    manifest.setdefault("partitions", {})
    manifest["partitions"][day] = {
        "file": f"{day}/data.jsonl", "rows": count,
        "first_ts": first_ts, "last_ts": last_ts,
        "bytes": path.stat().st_size, "sha256": digest,
    }
    tmp = mpath.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(_canonical(manifest))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, mpath)
    dir_fd = os.open(kind_dir, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


class MarketDatasetReader:
    """读取共享数据集（按时间区间；损坏 fail-closed）。"""

    def __init__(self, root: str | Path, *, venue: str, symbol: str, kind: str) -> None:
        _validate_key(venue, symbol, kind)
        self._venue, self._symbol, self._kind = venue, symbol, kind
        self._kind_dir = Path(root) / venue / symbol / kind

    @property
    def exists(self) -> bool:
        return self._kind_dir.exists()

    def _partition_plan(self) -> tuple[dict[str, object] | None, tuple[PartitionInfo, ...]]:
        """返回 (manifest, 分区清单)。有 manifest 时以 manifest 为准并严格校验其结构。"""
        manifest = _load_manifest(self._kind_dir)
        if manifest is not None:
            out = []
            for day, entry in sorted((manifest.get("partitions") or {}).items()):
                assert isinstance(entry, dict)
                out.append(PartitionInfo(
                    day=day, rows=int(entry["rows"]), first_ts=entry.get("first_ts"),
                    last_ts=entry.get("last_ts"), bytes_=int(entry.get("bytes", 0)),
                    sha256=str(entry.get("sha256", ""))))
            return manifest, tuple(out)
        # 未定稿（写器仍在追加）：直接扫磁盘分区，保证数据写完即可查（不伪造内容、不伪造 hash）
        if not self._kind_dir.exists():
            return None, ()
        out = []
        for day_dir in sorted(p for p in self._kind_dir.iterdir() if p.is_dir()):
            data = day_dir / "data.jsonl"
            if not data.exists():
                continue
            scan = _scan_partition(
                data, key=(self._venue, self._symbol, self._kind), collect=False)
            out.append(PartitionInfo(day=day_dir.name, rows=scan.rows, first_ts=scan.first_ts,
                                       last_ts=scan.last_ts, bytes_=scan.bytes_, sha256=""))
        return None, tuple(out)

    def partitions(self) -> tuple[PartitionInfo, ...]:
        return self._partition_plan()[1]

    def read(self, *, start_ts: int | None = None, end_ts: int | None = None,
             limit: int | None = None) -> tuple[MarketRecord, ...]:
        """读取区间记录（含端点）；limit 有界。无分片 ⇒ 空元组（不伪造）。

        已定稿分片按 manifest 重新核验 sha256 / bytes / rows / first_ts / last_ts；
        逐行再验键、schema 与时间单调性。任何不一致 fail-closed。
        """
        key = (self._venue, self._symbol, self._kind)
        manifest, infos = self._partition_plan()
        entries = (manifest or {}).get("partitions") if manifest is not None else None
        result: list[MarketRecord] = []
        for info in infos:
            path = self._kind_dir / info.day / "data.jsonl"
            if not path.exists():
                raise MarketDatasetFormatError(f"manifest lists missing partition: {path}")
            scan = _scan_partition(path, key=key, collect=True)
            if entries is not None and info.sha256:
                _verify_attestation(entries[info.day], scan, path=path, day=info.day)
            for record in scan.records:
                if start_ts is not None and record.exchange_ts < start_ts:
                    continue
                if end_ts is not None and record.exchange_ts > end_ts:
                    continue
                result.append(record)
                if limit is not None and len(result) >= limit:
                    return tuple(result)
        return tuple(result)

    def gaps(self) -> tuple[tuple[str, str], ...]:
        """分片之间的 UTC 日缺口（数据缺失的日子；不插值）。"""
        days = [info.day for info in self.partitions()]
        if len(days) < 2:
            return ()
        gaps: list[tuple[str, str]] = []
        for prev, cur in zip(days, days[1:]):
            d0 = _dt.date.fromisoformat(prev)
            d1 = _dt.date.fromisoformat(cur)
            delta = (d1 - d0).days
            if delta > 1:
                cursor = d0 + _dt.timedelta(days=1)
                while cursor < d1:
                    gaps.append((cursor.isoformat(), "missing day partition"))
                    cursor += _dt.timedelta(days=1)
        return tuple(gaps)


__all__ = [
    "MARKET_DATASET_SCHEMA",
    "VALID_KINDS",
    "MarketDatasetError",
    "MarketDatasetFormatError",
    "MarketDatasetWriter",
    "MarketDatasetReader",
    "MarketRecord",
    "PartitionInfo",
    "utc_day",
]
