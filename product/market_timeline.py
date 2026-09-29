"""Market Timeline Projection（P0001.12 §1）。

只读投影：把既有 Owner 的市场事实按时间排成点序列（供 UI 画图）。
**不是新的 market truth**：不重算任何特征，缺失即 `Fact.unknown`（UNKNOWN 绝不画成 0）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from market.events.types import Milliseconds

from product.types import Fact


@dataclass(frozen=True, slots=True)
class MarketTimelinePoint:
    """时间线上的一点（字段全部来自既有 Owner 事实）。"""

    ts: Milliseconds
    best_bid: Fact
    best_ask: Fact
    mid: Fact
    spread: Fact
    microprice: Fact
    imbalance: Fact
    ofi: Fact
    book_health: Fact
    market_generation: Fact
    data_quality: Fact


@dataclass(frozen=True, slots=True)
class MarketTimeline:
    """有界时间线投影（`truncated` 明确告诉 UI 数据被裁剪，而不是悄悄丢）。"""

    points: tuple[MarketTimelinePoint, ...]
    bucket_ms: Milliseconds
    max_points: int
    window_ms: Milliseconds | None
    source_points: int
    truncated: bool
    notes: tuple[str, ...] = ()


def _get(obj: object | None, name: str) -> object | None:
    return None if obj is None else getattr(obj, name, None)


def point_from_state(state: object, *, ts: Milliseconds | None = None,
                     book_health: object | None = None,
                     market_generation: int | None = None) -> MarketTimelinePoint:
    """把一个 `MarketState`（或同形事实对象）映射成时间线点（纯搬运）。"""
    price = _get(state, "price")
    depth = _get(state, "depth")
    flow = _get(state, "flow")
    quality = _get(state, "quality")
    time = _get(state, "time")
    resolved_ts = ts if ts is not None else _get(time, "as_of_exchange_ts")
    if resolved_ts is None:
        raise ValueError("timeline point requires a timestamp (state.time.as_of_exchange_ts or ts=)")
    health = book_health if book_health is not None else _get(quality, "book_health")
    return MarketTimelinePoint(
        ts=int(resolved_ts),
        best_bid=Fact.of(_get(price, "best_bid")),
        best_ask=Fact.of(_get(price, "best_ask")),
        mid=Fact.of(_get(price, "mid")),
        spread=Fact.of(_get(price, "spread")),
        microprice=Fact.of(_get(price, "microprice")),
        imbalance=Fact.of(_get(depth, "l1_imbalance")),
        ofi=Fact.of(_get(flow, "ofi_1s")),
        book_health=Fact.of(getattr(health, "value", health)),
        market_generation=Fact.of(market_generation),
        data_quality=Fact.of(_get(quality, "completeness")),
    )


def project_timeline(
    states: Sequence[object],
    *,
    bucket_ms: Milliseconds,
    max_points: int,
    window_ms: Milliseconds | None = None,
    book_health: object | None = None,
    market_generation: int | None = None,
) -> MarketTimeline:
    """把 `MarketState` 序列投影成**有界**时间线。

    - 按 `bucket_ms` 分桶，每桶保留**最后一个**已知状态（显示用降采样；不插值、不重算）；
    - 只保留最近 `max_points` 个桶（`truncated` 标记被裁掉的历史）；
    - `window_ms` 为显示窗口（只用于裁剪，不改变事实）。
    """
    if bucket_ms <= 0 or max_points <= 0:
        raise ValueError("bucket_ms / max_points must be > 0")
    if not states:
        return MarketTimeline(points=(), bucket_ms=int(bucket_ms), max_points=int(max_points),
                              window_ms=window_ms, source_points=0, truncated=False,
                              notes=("no market states recorded yet",))
    points = [point_from_state(state, book_health=book_health, market_generation=market_generation)
              for state in states]
    points.sort(key=lambda item: item.ts)
    if window_ms is not None:
        floor = points[-1].ts - int(window_ms)
        points = [item for item in points if item.ts >= floor]
    buckets: dict[int, MarketTimelinePoint] = {}
    for point in points:
        buckets[point.ts // int(bucket_ms)] = point
    ordered = [buckets[key] for key in sorted(buckets)]
    source_points = len(points)
    truncated = len(ordered) > int(max_points)
    if truncated:
        ordered = ordered[-int(max_points):]
    notes = (
        "projection is read-only; UNKNOWN stays UNKNOWN (never plotted as 0)",
        "downsampling keeps the last known state per bucket (display only)",
    )
    return MarketTimeline(points=tuple(ordered), bucket_ms=int(bucket_ms), max_points=int(max_points),
                          window_ms=window_ms, source_points=source_points, truncated=truncated,
                          notes=notes)


__all__ = ["MarketTimeline", "MarketTimelinePoint", "point_from_state", "project_timeline"]
