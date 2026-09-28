"""WS stream 名称、订阅报文与消息信封（P0001.9.1 §0.3）。

- stream 名称与 tier 归属集中在此（`@depth` → `public`；`@aggTrade` / `@markPrice` → `market`）；
- 订阅用 Binance 的 `SUBSCRIBE` 方法（带 `id`），ack 不计入业务事件；
- 信封解析严格：组合流 `{"stream":..,"data":..}` 与单流 `{"e":..}` 都必须能识别，
  ack / 错误对象 / 非 JSON 一律走异常路径（失败可见，不静默）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum

from connectors.binance.market_data.endpoints import StreamTier
from connectors.binance.market_data.errors import MarketDataFormatError

#: depth 增量推送速度。
#:
#: 实测（2026-09-28，真实 `/public` tier）：
#: - `@depth@100ms` 有数据，但 U/u 空洞最多（几乎每条消息都有 12–206 个缺失 id）；
#: - `@depth@250ms` **无数据**（不是合法后缀，订阅会 ACK 但不推流）；
#: - `@depth@500ms` 有数据且空洞更少；
#: - `@depth`（不带后缀 = 默认 250ms 推送）有数据。
DEPTH_SPEED_100MS = "100ms"
DEPTH_SPEED_500MS = "500ms"
#: 不带速度后缀：使用 Binance 默认推送速度。
DEPTH_SPEED_DEFAULT = ""
#: mark price 推送速度。
MARK_PRICE_SPEED_1S = "1s"


class StreamKind(Enum):
    """本阶段消费的 stream 种类。"""

    DEPTH = "depth"
    AGG_TRADE = "aggTrade"
    MARK_PRICE = "markPrice"

    @property
    def tier(self) -> StreamTier:
        """该 stream 在 2026 迁移后所属的 tier（订阅到错误 tier 会 ACK 但不推数据）。"""
        if self is StreamKind.DEPTH:
            return StreamTier.PUBLIC
        return StreamTier.MARKET


def depth_stream(symbol: str, *, speed: str = DEPTH_SPEED_100MS) -> str:
    """`<symbol>@depth[@<speed>]`；`speed` 为空字符串时使用 Binance 默认速度。"""
    base = f"{_require_symbol(symbol)}@depth"
    return base if not speed else f"{base}@{speed}"


def agg_trade_stream(symbol: str) -> str:
    return f"{_require_symbol(symbol)}@aggTrade"


def mark_price_stream(symbol: str, *, speed: str = MARK_PRICE_SPEED_1S) -> str:
    return f"{_require_symbol(symbol)}@markPrice@{speed}"


def streams_for(symbol: str, kinds: tuple[StreamKind, ...]) -> tuple[str, ...]:
    """按种类生成 stream 名称（顺序稳定，便于确定性比较）。"""
    builders = {
        StreamKind.DEPTH: depth_stream,
        StreamKind.AGG_TRADE: agg_trade_stream,
        StreamKind.MARK_PRICE: mark_price_stream,
    }
    return tuple(builders[kind](symbol) for kind in kinds)


def subscribe_message(streams: tuple[str, ...], *, request_id: int) -> str:
    """Binance `SUBSCRIBE` 报文（JSON 文本）。"""
    if not streams:
        raise ValueError("streams must not be empty")
    if isinstance(request_id, bool) or not isinstance(request_id, int) or request_id < 1:
        raise ValueError(f"request_id must be an int >= 1, got {request_id!r}")
    return json.dumps({"method": "SUBSCRIBE", "params": list(streams), "id": request_id}, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class StreamMessage:
    """一条业务消息（已剥离组合流信封）。"""

    stream: str | None
    data: dict[str, object]


def parse_message(text: str) -> StreamMessage | None:
    """解析一条 WS 文本消息。

    返回 `None` 表示这是订阅 ack / 控制响应（不是业务事件）；
    非 JSON、非对象、或既无 `data` 也无 `e` 的消息一律抛 `MarketDataFormatError`。
    """
    if not isinstance(text, str) or not text:
        raise MarketDataFormatError("ws message must be a non-empty string")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MarketDataFormatError(f"ws message is not valid JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise MarketDataFormatError(f"ws message must be an object, got {type(raw).__name__}")
    if "result" in raw or "error" in raw:
        return None
    if "data" in raw:
        data = raw["data"]
        if not isinstance(data, dict):
            raise MarketDataFormatError("ws message.data must be an object")
        stream = raw.get("stream")
        if stream is not None and not isinstance(stream, str):
            raise MarketDataFormatError("ws message.stream must be a string when present")
        return StreamMessage(stream=stream, data=data)
    if "e" in raw:
        return StreamMessage(stream=None, data=raw)
    raise MarketDataFormatError("ws message has neither 'data' nor event type 'e'")


def stream_matches(message: StreamMessage, expected: str) -> bool:
    """组合流信封里的 `stream` 字段与订阅名是否一致（单流消息无此字段则视为匹配）。"""
    return message.stream is None or message.stream == expected


def _require_symbol(symbol: str) -> str:
    if not isinstance(symbol, str) or not symbol:
        raise MarketDataFormatError("symbol must be a non-empty string")
    return symbol.lower()


__all__ = [
    "DEPTH_SPEED_100MS",
    "DEPTH_SPEED_500MS",
    "DEPTH_SPEED_DEFAULT",
    "MARK_PRICE_SPEED_1S",
    "StreamKind",
    "StreamMessage",
    "agg_trade_stream",
    "depth_stream",
    "mark_price_stream",
    "parse_message",
    "stream_matches",
    "streams_for",
    "subscribe_message",
]
