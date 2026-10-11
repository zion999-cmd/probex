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
VALID_KINDS = ("book", "trade", "mark")


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
        return _canonical(row)

    @staticmethod
    def from_row(row: object, *, path: str) -> "MarketRecord":
        if not isinstance(row, dict):
            raise MarketDatasetFormatError(f"{path}: record must be a JSON object")
        try:
            return MarketRecord(
                venue=row["venue"], symbol=row["symbol"], kind=row["kind"],
                exchange_ts=int(row["exchange_ts"]), receive_ts=int(row["receive_ts"]),
                source=str(row["source"]), payload=row.get("payload"),
                schema_version=str(row.get("schema_version", "")),
                quality=row.get("quality") if isinstance(row.get("quality"), dict) else None)
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

    @property
    def current_partition(self) -> str | None:
        return self._current_day

    def append(self, *, exchange_ts: int, receive_ts: int | None = None,
               payload: object = None, quality: dict[str, object] | None = None) -> MarketRecord:
        if not isinstance(exchange_ts, int) or exchange_ts <= 0:
            raise MarketDatasetError("exchange_ts must be a positive int (ms)")
        day = utc_day(exchange_ts)
        if self._handle is not None and day != self._current_day:
            self._finalize_partition()
        if self._handle is None:
            self._open_partition(day, exchange_ts)
        # 单调校验：同一分片不允许时间戳回退（防乱序悄悄改写历史顺序）
        if self._last_ts is not None and exchange_ts < self._last_ts:
            raise MarketDatasetError(
                f"out-of-order record for {self._symbol}/{self._kind}: {exchange_ts} < {self._last_ts}")
        record = MarketRecord(
            venue=self._venue, symbol=self._symbol, kind=self._kind,
            exchange_ts=exchange_ts, receive_ts=receive_ts or exchange_ts,
            source=self._source, payload=payload, quality=quality)
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
            # 续写：验证已有分片可读，恢复计数（fail closed on corruption）
            count = first = last = 0
            first_ts_seen: int | None = None
            for _line in path.read_text(encoding="utf-8").splitlines():
                if not _line.strip():
                    continue
                json.loads(_line)  # 损坏即抛
                count += 1
                obj = json.loads(_line)
                ts = int(obj["exchange_ts"])
                first_ts_seen = ts if first_ts_seen is None else first_ts_seen
                last = ts
            self._count_in_partition = count
            self._byte_in_partition = path.stat().st_size
            self._first_ts = first_ts_seen
            self._last_ts = last
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


def _manifest_path(kind_dir: Path) -> Path:
    return kind_dir / "manifest.json"


def _write_manifest_entry(*, kind_dir: Path, day: str, path: Path, count: int,
                           first_ts: int | None, last_ts: int | None) -> None:
    """原子更新 manifest（temp → fsync → replace → fsync(dir)）。"""
    kind_dir.mkdir(parents=True, exist_ok=True)
    mpath = _manifest_path(kind_dir)
    manifest = {}
    if mpath.exists():
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
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
        self._kind_dir = Path(root) / venue / symbol / kind

    @property
    def exists(self) -> bool:
        return self._kind_dir.exists()

    def partitions(self) -> tuple[PartitionInfo, ...]:
        mpath = _manifest_path(self._kind_dir)
        if not mpath.exists():
            return ()
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
        out = []
        for day, entry in sorted((manifest.get("partitions") or {}).items()):
            out.append(PartitionInfo(
                day=day, rows=int(entry["rows"]), first_ts=entry.get("first_ts"),
                last_ts=entry.get("last_ts"), bytes_=int(entry.get("bytes", 0)),
                sha256=str(entry.get("sha256", ""))))
        return tuple(out)

    def read(self, *, start_ts: int | None = None, end_ts: int | None = None,
             limit: int | None = None) -> tuple[MarketRecord, ...]:
        """读取区间记录（含端点）；limit 有界。无分片 ⇒ 空元组（不伪造）。"""
        result: list[MarketRecord] = []
        for info in self.partitions():
            path = self._kind_dir / info.day / "data.jsonl"
            if not path.exists():
                raise MarketDatasetFormatError(f"manifest lists missing partition: {path}")
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise MarketDatasetFormatError(f"{path}:{number}: corrupt JSON: {exc}") from None
                record = MarketRecord.from_row(row, path=str(path))
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
