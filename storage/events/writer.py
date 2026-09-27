"""EventWriter：append-only Event Store 的写入接口。

接口不绑定文件格式；`JsonlEventWriter` 只是当前 adapter。

契约：

- append-only：只追加，不提供修改 / 删除历史记录的入口。
- ordinal 由 store 分配，严格递增，绝不覆盖已有 ordinal：对已存在的文件继续写入时，
  先从已有记录续接 ordinal；已有内容非法则拒绝打开（fail closed）。
- 每条记录写入后立即 flush，crash 最多留下一条不完整行，读取端会将其判为损坏。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Self, TextIO

from market.events.types import MarketEvent

from storage.events.codec import SCHEMA_VERSION, EventRecord, compute_event_id, dumps_record
from storage.events.errors import EventStoreError
from storage.events.reader import JsonlEventReader


class EventWriter(ABC):
    """Event Store 写入接口。"""

    @property
    @abstractmethod
    def next_ordinal(self) -> int:
        """下一个将被分配的 ordinal。"""

    @abstractmethod
    def append(self, event: MarketEvent, *, source: str | None = None) -> EventRecord:
        """追加一条事件，返回落盘记录。"""

    @abstractmethod
    def close(self) -> None:
        """关闭写入端。幂等。"""

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


class JsonlEventWriter(EventWriter):
    """JSONL adapter：一行一条记录，追加写入。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._next_ordinal = _scan_next_ordinal(self._path)
        self._handle: TextIO | None = self._path.open("a", encoding="utf-8", newline="\n")

    @property
    def path(self) -> Path:
        return self._path

    @property
    def next_ordinal(self) -> int:
        return self._next_ordinal

    def append(self, event: MarketEvent, *, source: str | None = None) -> EventRecord:
        if self._handle is None:
            raise EventStoreError(f"{self._path}: writer is closed")
        if source is not None and (not isinstance(source, str) or not source):
            raise EventStoreError("source must be a non-empty string or None")

        record = EventRecord(
            ordinal=self._next_ordinal,
            event_id=compute_event_id(event),
            schema_version=SCHEMA_VERSION,
            event=event,
            source=source,
        )
        self._handle.write(dumps_record(record) + "\n")
        self._handle.flush()
        self._next_ordinal = record.ordinal + 1
        return record

    def close(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        handle.close()


def _scan_next_ordinal(path: Path) -> int:
    """从已有文件续接 ordinal；已有内容非法时 fail closed。"""
    if not path.exists():
        return 0
    last_ordinal: int | None = None
    for record in JsonlEventReader(path):
        last_ordinal = record.ordinal
    return 0 if last_ordinal is None else last_ordinal + 1
