"""测试辅助：用真实 Binance 报文构造统一 MarketEvent。

所有场景都经由归一化层（`parse_depth_snapshot` / `parse_depth_diff`）进入核心，
因此集成与故障测试同时覆盖 SC-1 的边界转换。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from connectors.binance.market_data import parse_depth_diff, parse_depth_snapshot
from market.book.market_book import BookUpdate, MarketBook
from market.events.types import MarketEvent

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


def feed(book: MarketBook, events: Sequence[MarketEvent]) -> list[BookUpdate]:
    """把事件依次投喂给 MarketBook，返回每次处理结果。"""
    return [book.on_market_event(event) for event in events]
