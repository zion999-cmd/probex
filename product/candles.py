"""Product-level candle aggregation（closure: chart workbench）。

从**已有** market 事实（`BoundedMarketHistory` 的 trades 与 book snapshots）做确定性的 OHLCV 聚合，
成为只读数据源；**前端不伪造 OHLC**。

纪律：

- 只搬运/聚合已记录事实，不产生新的市场真相；
- `source` 显式：`trades`（真实成交价）或 `mid_price`（无成交的桶用盘口 mid 补足，volume=0）；
- 桶按事件时间对齐（`bucket = ts // interval_ms * interval_ms`），**不使用 wall-clock**；
- 有界：`limit` 必填上限；超出 ⇒ `truncated=True`（不静默截断）。
"""

from __future__ import annotations

from dataclasses import dataclass

from market.events.types import Milliseconds

#: 允许的 timeframes（产品级；无默认值以外的业务含义）
CANDLE_INTERVALS: dict[str, int] = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}
DEFAULT_CANDLE_LIMIT = 300
MAX_CANDLE_LIMIT = 1_000

SOURCE_TRADES = "trades"
SOURCE_MID = "mid_price"


class CandleError(ValueError):
    """candle 参数错误（未知 interval / 非法 limit）。"""


@dataclass(frozen=True, slots=True)
class Candle:
    ts: Milliseconds
    open: float
    high: float
    low: float
    close: float
    volume: float
    source: str


@dataclass(frozen=True, slots=True)
class CandleSeries:
    interval: str
    interval_ms: int
    source: str
    candles: tuple[Candle, ...]
    truncated: bool
    total_buckets: int
    note: str


def _mid(snapshot: object) -> float | None:
    bids = getattr(snapshot, "bids", None) or ()
    asks = getattr(snapshot, "asks", None) or ()
    if not bids or not asks:
        return None
    try:
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
    except (TypeError, ValueError, IndexError):
        return None
    if best_bid <= 0 or best_ask <= 0:
        return None
    return (best_bid + best_ask) / 2.0


def _trades_by_bucket(trades: object, interval_ms: int) -> dict[int, list[tuple[int, float, float]]]:
    grouped: dict[int, list[tuple[int, float, float]]] = {}
    for trade in trades or ():
        ts = getattr(trade, "ts", None)
        price = getattr(trade, "price", None)
        quantity = getattr(trade, "quantity", None)
        if ts is None or price is None or quantity is None:
            continue
        try:
            ts_i, price_f, qty_f = int(ts), float(price), float(quantity)
        except (TypeError, ValueError):
            continue
        if price_f <= 0 or qty_f <= 0:
            continue
        grouped.setdefault((ts_i // interval_ms) * interval_ms, []).append((ts_i, price_f, qty_f))
    return grouped


def _mids_by_bucket(snapshots: object, interval_ms: int) -> dict[int, list[tuple[int, float]]]:
    grouped: dict[int, list[tuple[int, float]]] = {}
    for snapshot in snapshots or ():
        ts = getattr(snapshot, "ts", None)
        if ts is None:
            continue
        mid = _mid(snapshot)
        if mid is None:
            continue
        ts_i = int(ts)
        grouped.setdefault((ts_i // interval_ms) * interval_ms, []).append((ts_i, mid))
    return grouped


def aggregate_candles(*, trades: object, snapshots: object, interval: str,
                      limit: int = DEFAULT_CANDLE_LIMIT) -> CandleSeries:
    """把 trade/snapshot 事实聚合成有界 OHLCV 序列（确定性、按事件时间）。"""
    if interval not in CANDLE_INTERVALS:
        raise CandleError(f"unknown candle interval {interval!r} (allowed: {sorted(CANDLE_INTERVALS)})")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise CandleError("candle limit must be a positive int")
    if limit > MAX_CANDLE_LIMIT:
        raise CandleError(f"candle limit {limit} exceeds the safe maximum {MAX_CANDLE_LIMIT}")
    interval_ms = CANDLE_INTERVALS[interval]
    trades_by_bucket = _trades_by_bucket(trades, interval_ms)
    mids_by_bucket = _mids_by_bucket(snapshots, interval_ms)

    buckets = sorted(set(trades_by_bucket) | set(mids_by_bucket))
    candles: list[Candle] = []
    used_trades = False
    for bucket in buckets:
        points = trades_by_bucket.get(bucket)
        if points:
            points = sorted(points)
            prices = [price for _, price, _ in points]
            volume = sum(qty for _, _, qty in points)
            candles.append(Candle(ts=bucket, open=prices[0], high=max(prices), low=min(prices),
                                  close=prices[-1], volume=volume, source=SOURCE_TRADES))
            used_trades = True
            continue
        mids = sorted(mids_by_bucket.get(bucket) or ())
        if not mids:
            continue
        # 无成交的桶：用盘口 mid 补足（open/high/low/close 同值），volume=0（不伪造成交量）
        value = mids[-1][1]
        candles.append(Candle(ts=bucket, open=mids[0][1], high=max(m for _, m in mids),
                              low=min(m for _, m in mids), close=value, volume=0.0,
                              source=SOURCE_MID))

    total = len(candles)
    truncated = total > limit
    if truncated:
        candles = candles[-limit:]
    return CandleSeries(
        interval=interval, interval_ms=interval_ms,
        source=(SOURCE_TRADES if used_trades else SOURCE_MID),
        candles=tuple(candles), truncated=truncated, total_buckets=total,
        note=("OHLCV aggregated from recorded trades" if used_trades
              else "no trades recorded; OHLC built from book mid (volume omitted)"))


__all__ = ["CANDLE_INTERVALS", "Candle", "CandleError", "CandleSeries", "DEFAULT_CANDLE_LIMIT",
           "MAX_CANDLE_LIMIT", "SOURCE_MID", "SOURCE_TRADES", "aggregate_candles"]
