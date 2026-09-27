"""Binance USDⓈ-M 深度报文 -> 统一 MarketEvent。

只做归一化，不做网络。传输层（未包含在 P0001.1）负责：

1. 打开 WS depth stream，把 `depthUpdate` 原始报文交给 `parse_depth_diff`；
2. 通过 REST 拉取深度快照，把响应体连同请求的 symbol 交给 `parse_depth_snapshot`；
3. 把两者的 `receive_ts` / `process_ts` 填成当前时钟。
"""

from __future__ import annotations

from market.events.errors import InvalidPayloadError, MarketEventError
from market.events.payloads import BookDeltaPayload, BookSnapshotPayload, PriceLevel
from market.events.types import EventType, MarketEvent, Milliseconds, Venue

from connectors.binance.market_data.errors import MarketDataFormatError
from connectors.binance.market_data.parsing import (
    require_field,
    require_int,
    require_levels,
    require_mapping,
    require_str,
)

BINANCE_VENUE = Venue.BINANCE

_DEPTH_UPDATE = "depthUpdate"
_DIFF_PATH = "depthUpdate"
_SNAPSHOT_PATH = "depthSnapshot"


def parse_depth_diff(
    raw: object,
    *,
    receive_ts: Milliseconds,
    process_ts: Milliseconds,
) -> MarketEvent:
    """解析 WS `<symbol>@depth` 增量报文。"""
    message = require_mapping(raw, path=_DIFF_PATH)
    event_name = require_str(require_field(message, "e", path=_DIFF_PATH), path=f"{_DIFF_PATH}.e")
    if event_name != _DEPTH_UPDATE:
        raise MarketDataFormatError(f"{_DIFF_PATH}.e: expected {_DEPTH_UPDATE!r}, got {event_name!r}")

    symbol = require_str(require_field(message, "s", path=_DIFF_PATH), path=f"{_DIFF_PATH}.s")
    exchange_ts = require_int(require_field(message, "E", path=_DIFF_PATH), path=f"{_DIFF_PATH}.E")
    first_update_id = require_int(require_field(message, "U", path=_DIFF_PATH), path=f"{_DIFF_PATH}.U")
    last_update_id = require_int(require_field(message, "u", path=_DIFF_PATH), path=f"{_DIFF_PATH}.u")
    bids = require_levels(require_field(message, "b", path=_DIFF_PATH), path=f"{_DIFF_PATH}.b")
    asks = require_levels(require_field(message, "a", path=_DIFF_PATH), path=f"{_DIFF_PATH}.a")

    payload = _build_delta_payload(first_update_id, last_update_id, bids, asks)
    return _build_event(
        path=_DIFF_PATH,
        symbol=symbol,
        event_type=EventType.BOOK_DELTA,
        exchange_ts=exchange_ts,
        receive_ts=receive_ts,
        process_ts=process_ts,
        payload=payload,
    )


def parse_depth_snapshot(
    raw: object,
    *,
    symbol: str,
    receive_ts: Milliseconds,
    process_ts: Milliseconds,
) -> MarketEvent:
    """解析 REST 深度快照响应体。快照不带 symbol，必须由请求方提供。"""
    if not isinstance(symbol, str) or not symbol:
        raise MarketDataFormatError("symbol: expected a non-empty string for the depth snapshot request")

    message = require_mapping(raw, path=_SNAPSHOT_PATH)
    last_update_id = require_int(
        require_field(message, "lastUpdateId", path=_SNAPSHOT_PATH), path=f"{_SNAPSHOT_PATH}.lastUpdateId"
    )
    exchange_ts = require_int(require_field(message, "E", path=_SNAPSHOT_PATH), path=f"{_SNAPSHOT_PATH}.E")
    bids = require_levels(require_field(message, "bids", path=_SNAPSHOT_PATH), path=f"{_SNAPSHOT_PATH}.bids")
    asks = require_levels(require_field(message, "asks", path=_SNAPSHOT_PATH), path=f"{_SNAPSHOT_PATH}.asks")

    payload = _build_snapshot_payload(last_update_id, bids, asks)
    return _build_event(
        path=_SNAPSHOT_PATH,
        symbol=symbol,
        event_type=EventType.BOOK_SNAPSHOT,
        exchange_ts=exchange_ts,
        receive_ts=receive_ts,
        process_ts=process_ts,
        payload=payload,
    )


def _build_event(
    *,
    path: str,
    symbol: str,
    event_type: EventType,
    exchange_ts: Milliseconds,
    receive_ts: Milliseconds,
    process_ts: Milliseconds,
    payload: BookSnapshotPayload | BookDeltaPayload,
) -> MarketEvent:
    """所有边界错误统一为 MarketDataFormatError，核心不被部分构造的事件污染。"""
    try:
        return MarketEvent(
            venue=BINANCE_VENUE,
            symbol=symbol,
            event_type=event_type,
            exchange_ts=exchange_ts,
            receive_ts=receive_ts,
            process_ts=process_ts,
            sequence=payload.last_update_id,
            payload=payload,
        )
    except MarketEventError as exc:
        raise MarketDataFormatError(f"{path}: {exc}") from exc


def _build_delta_payload(
    first_update_id: int,
    last_update_id: int,
    bids: tuple[PriceLevel, ...],
    asks: tuple[PriceLevel, ...],
) -> BookDeltaPayload:
    try:
        return BookDeltaPayload(
            first_update_id=first_update_id,
            last_update_id=last_update_id,
            bids=bids,
            asks=asks,
        )
    except InvalidPayloadError as exc:
        raise MarketDataFormatError(f"{_DIFF_PATH}: {exc}") from exc


def _build_snapshot_payload(
    last_update_id: int,
    bids: tuple[PriceLevel, ...],
    asks: tuple[PriceLevel, ...],
) -> BookSnapshotPayload:
    try:
        return BookSnapshotPayload(last_update_id=last_update_id, bids=bids, asks=asks)
    except InvalidPayloadError as exc:
        raise MarketDataFormatError(f"{_SNAPSHOT_PATH}: {exc}") from exc
