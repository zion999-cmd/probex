"""测试辅助：构造真实 Binance 报文、写 Event Store、驱动 MarketBook。

所有场景都经由归一化层（`parse_depth_snapshot` / `parse_depth_diff`）进入核心，
因此集成与故障测试同时覆盖 P0001.1 的 SC-1 边界转换。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from connectors.binance.market_data import parse_depth_diff, parse_depth_snapshot
from market.book.market_book import BookUpdate, BookView, MarketBook
from market.events.types import MarketEvent, Venue
from market.health.state import HealthTransition
from storage.events.codec import SCHEMA_VERSION, EventRecord, compute_event_id, dumps_record
from storage.events.writer import JsonlEventWriter

SYMBOL = "BTCUSDT"
BASE_TS = 1_700_000_000_000

LevelPairs = Iterable[tuple[float, float]]


def _levels(pairs: LevelPairs) -> list[list[str]]:
    return [[str(price), str(size)] for price, size in pairs]


def depth_snapshot_event(
    last_update_id: int,
    *,
    symbol: str = SYMBOL,
    bids: LevelPairs = (),
    asks: LevelPairs = (),
    exchange_ts: int = BASE_TS,
    receive_ts: int = BASE_TS,
    process_ts: int = BASE_TS,
) -> MarketEvent:
    """构造 Binance USDⓈ-M REST 深度快照事件。"""
    raw = {
        "lastUpdateId": last_update_id,
        "E": exchange_ts,
        "T": exchange_ts,
        "bids": _levels(bids),
        "asks": _levels(asks),
    }
    return parse_depth_snapshot(raw, symbol=symbol, receive_ts=receive_ts, process_ts=process_ts)


def depth_diff_event(
    first_update_id: int,
    last_update_id: int,
    *,
    symbol: str = SYMBOL,
    bids: LevelPairs = (),
    asks: LevelPairs = (),
    exchange_ts: int = BASE_TS,
    receive_ts: int = BASE_TS,
    process_ts: int = BASE_TS,
) -> MarketEvent:
    """构造 Binance USDⓈ-M WS `depthUpdate` 增量事件。"""
    raw = {
        "e": "depthUpdate",
        "E": exchange_ts,
        "T": exchange_ts,
        "s": symbol,
        "U": first_update_id,
        "u": last_update_id,
        "b": _levels(bids),
        "a": _levels(asks),
    }
    return parse_depth_diff(raw, receive_ts=receive_ts, process_ts=process_ts)


def market_book() -> MarketBook:
    """新建一个针对 `SYMBOL` 的 MarketBook。"""
    return MarketBook(Venue.BINANCE, SYMBOL)


def feed(book: MarketBook, events: Sequence[MarketEvent]) -> list[BookUpdate]:
    """把事件依次投喂给 MarketBook，返回每次处理结果。"""
    return [book.on_market_event(event) for event in events]


@dataclass(frozen=True, slots=True)
class BookRun:
    """一次盘口驱动的完整记录，用于确定性比较。"""

    updates: tuple[BookUpdate, ...]
    transitions: tuple[HealthTransition, ...]
    final_view: BookView


def replay_into(book: MarketBook, events: Iterable[MarketEvent], *, depth: int = 1000) -> BookRun:
    """投喂事件流，并在需要时模拟传输层发出 resync 请求。

    Live 传输层不属于 P0001.1 / P0001.2 的范围，因此这里显式承担它的职责：
    一旦 `BookUpdate.resync_required` 为真，就调用 `request_resync()`，
    等价于真实场景中的重新订阅 + 重新拉取快照。
    """
    updates: list[BookUpdate] = []
    transitions: list[HealthTransition] = []
    for event in events:
        update = book.on_market_event(event)
        updates.append(update)
        if update.health_after is not update.health_before:
            transition = book.last_transition
            assert transition is not None
            transitions.append(transition)
        if update.resync_required:
            book.request_resync()
    return BookRun(updates=tuple(updates), transitions=tuple(transitions), final_view=book.view(depth=depth))


def record_for(event: MarketEvent, *, ordinal: int = 0, source: str | None = None) -> EventRecord:
    """构造一条合法记录（`event_id` 由事件内容派生）。"""
    return EventRecord(
        ordinal=ordinal,
        event_id=compute_event_id(event),
        schema_version=SCHEMA_VERSION,
        event=event,
        source=source,
    )


def record_line(record: EventRecord) -> str:
    """一行 JSONL（含换行符）。"""
    return dumps_record(record) + "\n"


def record_raw(record: EventRecord) -> dict[str, object]:
    """记录的 JSON 结构深拷贝，便于篡改。"""
    return json.loads(dumps_record(record))


def write_store(
    path: Path,
    events: Iterable[MarketEvent],
    *,
    source: str | None = None,
) -> tuple[EventRecord, ...]:
    """把事件写入 JSONL Event Store，返回落盘记录。"""
    records: list[EventRecord] = []
    writer = JsonlEventWriter(path)
    try:
        for event in events:
            records.append(writer.append(event, source=source))
    finally:
        writer.close()
    return tuple(records)


class TempDirTestCase(unittest.TestCase):
    """提供临时目录的测试基类。"""

    tmp_path: Path

    def setUp(self) -> None:
        super().setUp()
        self.tmp_path = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def store_path(self, name: str = "events.jsonl") -> Path:
        return self.tmp_path / name

