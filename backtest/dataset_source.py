"""回测输入的统一数据来源：event-store JSONL 文件或 P0017.1 共享市场数据集目录。

两种形态
--------
- **event-store 文件**（既有）：`JsonlEventReader` 顺序读出的 `EventRecord`。
- **market-dataset 目录**（新增）：`<root>/<venue>/<symbol>/<book|trade|mark>/<day>/data.jsonl`，
  每行 payload 是 `storage.events.codec.encode_event` 的 canonical 事件字典；
  经共享数据集 manifest 完整性证明（sha256/bytes/rows/首末 ts）后解码。

合并语义（dataset 目录）
-----------------------
跨 kind 的同一毫秒顺序在采集落盘后无法逐 ordinal 还原，因此使用**确定性优先级**：

    (exchange_ts, 事件类型优先级, symbol, receive_ts, sequence)

快照 < 增量 < mark < 成交。同一 (symbol, kind) 内部严格保持文件顺序（分片按日有序）。
任何路径键与事件内容（venue/symbol/event_type↔kind）不符 ⇒ INVALID_INPUT（fail-closed）。

本模块只读、确定性、无 wall-clock；不改变任何交易语义。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from backtest import BacktestError, BacktestStatus
from market.events.types import EventType, MarketEvent
from storage.events.codec import (
    SCHEMA_VERSION,
    EventRecord,
    compute_event_id,
    decode_event,
)
from storage.market_dataset import MarketDatasetReader

DATASET_KINDS = ("book", "trade", "mark", "bar")

#: 同一毫秒跨 kind 的确定性先后（无 ordinal 时的显式规则）
TYPE_PRIORITY = {
    EventType.BOOK_SNAPSHOT: 0,
    EventType.BOOK_DELTA: 1,
    EventType.MARK_PRICE: 2,
    EventType.TRADE: 3,
    EventType.BAR: 4,
}
KIND_FOR_EVENT_TYPE = {
    EventType.BOOK_SNAPSHOT: "book",
    EventType.BOOK_DELTA: "book",
    EventType.TRADE: "trade",
    EventType.MARK_PRICE: "mark",
    EventType.BAR: "bar",
}
_EVENT_TYPES_FOR_KIND = {
    "book": (EventType.BOOK_SNAPSHOT, EventType.BOOK_DELTA),
    "trade": (EventType.TRADE,),
    "mark": (EventType.MARK_PRICE,),
    "bar": (EventType.BAR,),
}


@dataclass(frozen=True, slots=True)
class DatasetSymbol:
    venue: str
    symbol: str


def _fail(detail: str, *, location: str = "") -> BacktestError:
    return BacktestError(BacktestStatus.INVALID_INPUT, detail,
                          location=location)


def discover_dataset_symbols(root: Path) -> tuple[DatasetSymbol, ...]:
    """枚举 `<root>/<venue>/<symbol>`（至少含一个合法 kind 目录）；无布局 ⇒ 空。"""
    found: list[DatasetSymbol] = []
    if not root.is_dir():
        return ()
    for venue_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for symbol_dir in sorted(p for p in venue_dir.iterdir() if p.is_dir()):
            if any((symbol_dir / kind).is_dir() for kind in DATASET_KINDS):
                found.append(DatasetSymbol(venue=venue_dir.name, symbol=symbol_dir.name))
    return tuple(found)


def is_dataset_dir(path: Path) -> bool:
    return path.is_dir() and bool(discover_dataset_symbols(path))


def _records_for_symbol(root: Path, item: DatasetSymbol) -> list[tuple]:
    """读一个 symbol 的全部 kind ⇒ 带排序键的 canonical 事件（严格校验）。

    排序键：有采集序号 ⇒ `(ts, 0, ordinal)`（复现真实到达顺序，跨 kind 同毫秒也确定）；
    无序号（旧数据）⇒ `(ts, 1, 类型优先级, receive_ts)`。
    """
    keyed: list[tuple] = []
    for kind in DATASET_KINDS:
        reader = MarketDatasetReader(root, venue=item.venue, symbol=item.symbol, kind=kind)
        if not reader.exists:
            continue
        allowed = _EVENT_TYPES_FOR_KIND[kind]
        try:
            dataset_records = reader.read()
        except Exception as exc:  # noqa: BLE001 - 存储层损坏 ⇒ 回测输入 INVALID_INPUT
            raise _fail(f"market dataset {item.venue}/{item.symbol}/{kind} unreadable: "
                        f"{type(exc).__name__}: {exc}") from None
        for record in dataset_records:
            try:
                event = decode_event(record.payload)
            except Exception as exc:  # noqa: BLE001 - payload 不是 canonical 事件字典
                raise _fail(f"{item.venue}/{item.symbol}/{kind}@{record.exchange_ts}: "
                            f"payload is not a canonical event: {type(exc).__name__}: {exc}") from None
            where = f"{item.venue}/{item.symbol}/{kind}@{record.exchange_ts}"
            if event.venue.value != item.venue:
                raise _fail(f"{where}: event venue {event.venue.value!r} != directory {item.venue!r}")
            if event.symbol != item.symbol:
                raise _fail(f"{where}: event symbol {event.symbol!r} != directory {item.symbol!r}")
            if event.event_type not in allowed:
                raise _fail(f"{where}: event_type {event.event_type.value!r} not allowed in kind {kind!r}")
            ts = int(event.exchange_ts)
            if record.ordinal is not None:
                order_key = (ts, 0, int(record.ordinal), 0)
            else:
                order_key = (ts, 1, TYPE_PRIORITY[event.event_type], int(event.receive_ts))
            keyed.append((*order_key, item.symbol, record, event))
    keyed.sort(key=lambda k: k[:4])
    return keyed


def load_dataset_records(path: Path, *, symbols: tuple[str, ...] | None = None
                           ) -> tuple[EventRecord, ...]:
    """读共享数据集目录，返回全局有序的 EventRecord（ordinal 按合并顺序重新编号）。"""
    items = discover_dataset_symbols(path)
    if not items:
        raise _fail(f"dataset directory has no <venue>/<symbol>/{DATASET_KINDS} layout: {path}")
    wanted = set(symbols) if symbols is not None else None
    selected = tuple(item for item in items if wanted is None or item.symbol in wanted)
    if wanted is not None:
        missing = sorted(wanted - {item.symbol for item in selected})
        if missing:
            raise _fail(f"dataset {path} has no data for declared symbol(s) {missing}")
    keyed: list[tuple] = []
    for item in selected:
        keyed.extend(_records_for_symbol(path, item))
    keyed.sort(key=lambda k: k[:4])
    out: list[EventRecord] = []
    last_ts: int | None = None
    for ordinal, (_ts, _rank, _order, _tie, _symbol, raw, event) in enumerate(keyed):
        if last_ts is not None and event.exchange_ts < last_ts:
            raise _fail("event timeline goes backwards after ordering")
        last_ts = int(event.exchange_ts)
        out.append(EventRecord(ordinal=ordinal, event_id=compute_event_id(event),
                                schema_version=SCHEMA_VERSION, event=event,
                                source=f"market-dataset:{raw.source}"))
    return tuple(out)


def load_records(path: str | Path, *, symbols: tuple[str, ...] | None = None
                  ) -> tuple[EventRecord, ...]:
    """统一入口：文件 ⇒ event-store；目录 ⇒ 共享数据集（按 symbols 过滤/校验）。"""
    root = Path(path)
    if root.is_file():
        from storage.events.reader import JsonlEventReader

        return tuple(JsonlEventReader(root))
    if root.is_dir():
        return load_dataset_records(root, symbols=symbols)
    raise _fail(f"dataset not found: {root}")


__all__ = [
    "DATASET_KINDS",
    "DatasetSymbol",
    "discover_dataset_symbols",
    "is_dataset_dir",
    "load_dataset_records",
    "load_records",
]
