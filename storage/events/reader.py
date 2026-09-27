"""EventReader：append-only Event Store 的读取接口。

接口不绑定文件格式；`JsonlEventReader` 只是当前 adapter。

契约：

- `__iter__` 必须可重复调用（每次返回从头开始的独立迭代器），`reset()` 语义依赖这一点。
- 迭代顺序就是 store 的 recorded ordinal 顺序，即 replay 的 canonical 顺序。
- `ordinal` 必须严格递增：重复或逆序一律抛出 `EventOrdinalError`（fail closed）。
- 任何行结构非法都抛出 `EventStoreError` 子类并带上行号，不静默跳过。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path

from storage.events.codec import EventRecord, loads_record
from storage.events.errors import EventOrdinalError, EventStoreError


class EventReader(ABC):
    """Event Store 读取接口。"""

    @abstractmethod
    def __iter__(self) -> Iterator[EventRecord]:
        """按 recorded ordinal 顺序迭代记录。"""

    def read_all(self) -> tuple[EventRecord, ...]:
        """一次性读完所有记录。"""
        return tuple(self)


class JsonlEventReader(EventReader):
    """JSONL adapter：一行一条记录。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def __iter__(self) -> Iterator[EventRecord]:
        with self._path.open("r", encoding="utf-8") as handle:
            previous_ordinal: int | None = None
            for lineno, line in enumerate(handle, start=1):
                record = self._parse_line(line, lineno)
                if previous_ordinal is not None and record.ordinal <= previous_ordinal:
                    raise EventOrdinalError(
                        f"{self._path}: line {lineno}: ordinal {record.ordinal} is not strictly greater "
                        f"than previous ordinal {previous_ordinal}"
                    )
                previous_ordinal = record.ordinal
                yield record

    def _parse_line(self, line: str, lineno: int) -> EventRecord:
        try:
            return loads_record(line)
        except EventStoreError as exc:
            raise type(exc)(f"{self._path}: line {lineno}: {exc}") from exc
