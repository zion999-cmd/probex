"""Equity / Exposure timeline projection（P0001.12.2 / G3）。

只读投影：把**已记录**的账户/敞口样本按时间排成有界序列（供 Performance 画 equity curve 与 exposure 曲线）。
不重算权益或盈亏：样本值来自既有 Owner（AccountingCore / OrderTracker）。
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field

from market.events.types import Milliseconds

from product.market_projection import MarketProjectionConfig, ProjectionError
from product.types import Fact


@dataclass(frozen=True, slots=True)
class AccountSample:
    """一个**已知**账户采样点（未提供的字段保持 UNKNOWN，不填 0）。"""

    ts: Milliseconds
    equity: float | None = None
    balance: float | None = None
    position_qty: float | None = None
    exposure_total: float | None = None
    exposure_confirmed: float | None = None
    #: closure slice：既有 accounting 事实（Monitor/Performance 的 PnL 趋势）
    unrealized_pnl: float | None = None
    realized_pnl: float | None = None


@dataclass(frozen=True, slots=True)
class AccountTimelinePoint:
    ts: Milliseconds
    equity: Fact
    balance: Fact
    position_qty: Fact
    exposure_total: Fact
    exposure_confirmed: Fact
    unrealized_pnl: Fact = field(default_factory=lambda: Fact.unknown("unrealized pnl not recorded"))
    realized_pnl: Fact = field(default_factory=lambda: Fact.unknown("realized pnl not recorded"))


@dataclass(frozen=True, slots=True)
class AccountTimeline:
    points: tuple[AccountTimelinePoint, ...]
    bucket_ms: Milliseconds
    window_ms: Milliseconds
    max_points: int
    source_points: int
    truncated: bool
    notes: tuple[str, ...] = ()


@dataclass(slots=True)
class BoundedAccountTimeline:
    """有界账户样本缓冲（运行时把事实喂进来；不是新的 accounting Owner）。"""

    capacity: int
    run_id: str | None = None
    _samples: deque = field(default_factory=deque)

    def __post_init__(self) -> None:
        if isinstance(self.capacity, bool) or not isinstance(self.capacity, int) or self.capacity <= 0:
            raise ProjectionError("BoundedAccountTimeline.capacity must be a positive int")
        self._samples = deque(self._samples, maxlen=self.capacity)

    def feed(self, sample: AccountSample) -> None:
        self._samples.append(sample)

    @property
    def counts(self) -> dict[str, int]:
        return {"samples": len(self._samples), "capacity": self.capacity}

    def samples(self) -> tuple[AccountSample, ...]:
        return tuple(self._samples)


def project_account_timeline(samples: Sequence[AccountSample], *,
                             config: MarketProjectionConfig) -> AccountTimeline:
    """按显示上限投影账户序列（分桶保留**最后一个**已知样本；缺字段保持 UNKNOWN）。"""
    if not isinstance(config, MarketProjectionConfig):
        raise ProjectionError("project_account_timeline requires a MarketProjectionConfig")
    ordered = sorted(samples, key=lambda item: item.ts)
    if not ordered:
        return AccountTimeline(points=(), bucket_ms=config.bucket_ms, window_ms=config.window_ms,
                               max_points=config.max_points, source_points=0, truncated=False,
                               notes=("no account samples recorded yet",))
    floor = ordered[-1].ts - config.window_ms
    ordered = [item for item in ordered if item.ts >= floor]
    buckets: dict[int, AccountSample] = {}
    for sample in ordered:
        buckets[sample.ts // config.bucket_ms] = sample
    keys = sorted(buckets)
    truncated = len(keys) > config.max_points
    if truncated:
        keys = keys[-config.max_points:]
    points = tuple(
        AccountTimelinePoint(
            ts=buckets[key].ts,
            equity=Fact.of(buckets[key].equity),
            balance=Fact.of(buckets[key].balance),
            position_qty=Fact.of(buckets[key].position_qty),
            exposure_total=Fact.of(buckets[key].exposure_total),
            exposure_confirmed=Fact.of(buckets[key].exposure_confirmed),
            unrealized_pnl=Fact.of(buckets[key].unrealized_pnl),
            realized_pnl=Fact.of(buckets[key].realized_pnl),
        )
        for key in keys
    )
    return AccountTimeline(points=points, bucket_ms=config.bucket_ms, window_ms=config.window_ms,
                           max_points=config.max_points, source_points=len(ordered), truncated=truncated,
                           notes=("account timeline is read-only; UNKNOWN stays UNKNOWN",))


__all__ = ["AccountSample", "AccountTimeline", "AccountTimelinePoint", "BoundedAccountTimeline",
           "project_account_timeline"]
