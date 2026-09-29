"""Market Projection（P0001.12 §2/§3/§6/§7/§8/§9/§10）与有界历史缓冲（§数据量与降采样）。

三件事，全部只读：

1. **Depth heatmap**：price × time × visible resting quantity（来自 Owner 的盘口快照）；
2. **Trades / Health / Overlays**：真实 aggressor trade、BookHealth/gap/resync/generation、MakerDecision、ExecutionEvent；
3. **`BoundedMarketHistory`**：运行时把事实喂进一个有界缓冲，API 从缓冲投影（**不是**新的 market truth）。

纪律（提案明文）：

- `L2 change ≠ fill evidence`（D-025）：盘口消失**绝不**解释成成交；本模块不产生任何 fill；
- replay control 只允许作用于 REPLAY runtime：`ReplayControl` 在非 REPLAY 模式下**拒绝**任何命令；
- 所有上限（window / bucket / max_points / price_levels）**显式配置**；`truncated` 明确告知 UI。
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum

from market.events.types import Milliseconds

from product.types import Fact, RuntimeMode

#: 允许的 replay 倍速（提案 §3）
ALLOWED_SPEEDS: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0, 5.0, 10.0)

DEPTH_NOTE = "depth disappearance is NOT fill evidence (D-025: L2 change != fill evidence)"


class ProjectionError(ValueError):
    """投影契约错误（参数非法 / 控制对象缺失）。"""


class ReplayControlError(RuntimeError):
    """replay 控制被拒绝（例如试图控制 TESTNET/LIVE）。"""


@dataclass(frozen=True, slots=True)
class MarketProjectionConfig:
    """显示参数（**只影响展示，不影响交易事实**）；全部显式，无默认值。"""

    window_ms: Milliseconds
    bucket_ms: Milliseconds
    max_points: int
    price_levels: int

    def __post_init__(self) -> None:
        for name in ("window_ms", "bucket_ms", "max_points", "price_levels"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ProjectionError(f"MarketProjectionConfig.{name} must be a positive int")


# ---------------------------------------------------------------------- depth

@dataclass(frozen=True, slots=True)
class DepthCell:
    """热图单元：某个时间桶里某个价位的可见挂单量。"""

    bucket_ts: Milliseconds
    price: float
    quantity: float
    side: str


@dataclass(frozen=True, slots=True)
class DepthHeatmap:
    cells: tuple[DepthCell, ...]
    bid_series: tuple[tuple[Milliseconds, float], ...]
    ask_series: tuple[tuple[Milliseconds, float], ...]
    mid_series: tuple[tuple[Milliseconds, float], ...]
    bucket_ms: Milliseconds
    price_levels: int
    max_points: int
    source_points: int
    truncated: bool
    notes: tuple[str, ...] = (DEPTH_NOTE,)


def project_depth(
    snapshots: Sequence[object],
    *,
    config: MarketProjectionConfig,
    max_snapshots: int | None = None,
) -> DepthHeatmap:
    """把盘口快照序列投影成有时间桶的热图（**有界**：桶数、每档层数都有限）。

    `snapshots` 元素需提供 `ts`、`bids`、`asks`（每项 `(price, size)`）；顺序无关（内部按 ts 排序）。
    本函数**只搬运可见挂单量**，不做任何成交推断。
    """
    if not isinstance(config, MarketProjectionConfig):
        raise ProjectionError("project_depth requires a MarketProjectionConfig")
    ordered = sorted(snapshots, key=lambda item: getattr(item, "ts"))
    if max_snapshots is not None and len(ordered) > int(max_snapshots):
        ordered = ordered[-int(max_snapshots):]
    source_points = len(ordered)
    if not ordered:
        return DepthHeatmap(cells=(), bid_series=(), ask_series=(), mid_series=(),
                            bucket_ms=config.bucket_ms, price_levels=config.price_levels,
                            max_points=config.max_points, source_points=0, truncated=False,
                            notes=(DEPTH_NOTE, "no book snapshots recorded yet"))
    floor = int(ordered[-1].ts) - config.window_ms
    ordered = [item for item in ordered if int(item.ts) >= floor]
    buckets: dict[int, object] = {}
    for snapshot in ordered:
        buckets[int(snapshot.ts) // config.bucket_ms] = snapshot
    keys = sorted(buckets)
    truncated = len(keys) > config.max_points
    if truncated:
        keys = keys[-config.max_points:]
    cells: list[DepthCell] = []
    bid_series: list[tuple[int, float]] = []
    ask_series: list[tuple[int, float]] = []
    mid_series: list[tuple[int, float]] = []
    for key in keys:
        snapshot = buckets[key]
        bucket_ts = key * config.bucket_ms
        bids = list(getattr(snapshot, "bids", ()))[:config.price_levels]
        asks = list(getattr(snapshot, "asks", ()))[:config.price_levels]
        for price, size in bids:
            cells.append(DepthCell(bucket_ts=bucket_ts, price=float(price), quantity=float(size), side="bid"))
        for price, size in asks:
            cells.append(DepthCell(bucket_ts=bucket_ts, price=float(price), quantity=float(size), side="ask"))
        if bids:
            bid_series.append((bucket_ts, float(bids[0][0])))
        if asks:
            ask_series.append((bucket_ts, float(asks[0][0])))
        if bids and asks:
            mid_series.append((bucket_ts, (float(bids[0][0]) + float(asks[0][0])) / 2.0))
    return DepthHeatmap(cells=tuple(cells), bid_series=tuple(bid_series), ask_series=tuple(ask_series),
                        mid_series=tuple(mid_series), bucket_ms=config.bucket_ms,
                        price_levels=config.price_levels, max_points=config.max_points,
                        source_points=source_points, truncated=truncated, notes=(DEPTH_NOTE,))


# ---------------------------------------------------------------------- trades

@dataclass(frozen=True, slots=True)
class TradePrint:
    """真实 aggressor 成交（只来自 `TradePayload`）。"""

    ts: Milliseconds
    price: float
    quantity: float
    aggressor: str


@dataclass(frozen=True, slots=True)
class TradeProjection:
    prints: tuple[TradePrint, ...]
    max_points: int
    source_points: int
    truncated: bool
    notes: tuple[str, ...] = ("trades come from TradePayload only, never inferred from book deltas",)


def project_trades(trades: Sequence[object], *, max_points: int) -> TradeProjection:
    """投影真实成交打印；**绝不**从盘口变化推断成交（SC-3）。"""
    if max_points <= 0:
        raise ProjectionError("max_points must be > 0")
    ordered = sorted(trades, key=lambda item: getattr(item, "ts"))
    source_points = len(ordered)
    truncated = source_points > int(max_points)
    if truncated:
        ordered = ordered[-int(max_points):]
    prints = tuple(
        TradePrint(ts=int(getattr(trade, "ts")), price=float(getattr(trade, "price")),
                   quantity=float(getattr(trade, "quantity")),
                   aggressor=str(getattr(getattr(trade, "aggressor"), "value", getattr(trade, "aggressor"))))
        for trade in ordered
    )
    return TradeProjection(prints=prints, max_points=int(max_points), source_points=source_points,
                           truncated=truncated)


# ---------------------------------------------------------------------- health

@dataclass(frozen=True, slots=True)
class HealthSegment:
    """BookHealth / gap / resync / generation 变化段（用于质量 overlay）。"""

    ts: Milliseconds
    health: str
    reason: str
    market_generation: int | None


@dataclass(frozen=True, slots=True)
class HealthProjection:
    segments: tuple[HealthSegment, ...]
    max_segments: int
    source_segments: int
    truncated: bool


def project_health(transitions: Sequence[object], *, max_segments: int) -> HealthProjection:
    """投影健康/质量变化（HEALTHY / STALE / gap / resync / generation change）。"""
    if max_segments <= 0:
        raise ProjectionError("max_segments must be > 0")
    ordered = sorted(transitions, key=lambda item: getattr(item, "ts"))
    source_segments = len(ordered)
    truncated = source_segments > int(max_segments)
    if truncated:
        ordered = ordered[-int(max_segments):]
    segments = tuple(
        HealthSegment(ts=int(getattr(item, "ts")),
                      health=str(getattr(getattr(item, "health"), "value", getattr(item, "health"))),
                      reason=str(getattr(item, "reason") or ""),
                      market_generation=getattr(item, "market_generation", None))
        for item in ordered
    )
    return HealthProjection(segments=segments, max_segments=int(max_segments),
                            source_segments=source_segments, truncated=truncated)


# ---------------------------------------------------------------------- overlays

@dataclass(frozen=True, slots=True)
class DecisionOverlay:
    ts: Milliseconds
    side: str
    action: str
    price: Fact
    quantity: Fact
    decision_id: Fact
    reason: Fact


@dataclass(frozen=True, slots=True)
class ExecutionOverlay:
    ts: Milliseconds
    client_order_id: str
    event: str
    detail: str


@dataclass(frozen=True, slots=True)
class OverlayProjection:
    decisions: tuple[DecisionOverlay, ...]
    executions: tuple[ExecutionOverlay, ...]
    max_points: int
    truncated: bool


def project_overlays(decisions: Sequence[object] = (), executions: Sequence[object] = (),
                     *, max_points: int) -> OverlayProjection:
    """把既有 MakerDecision / ExecutionEvent 叠加到时间线上（不推测状态）。"""
    if max_points <= 0:
        raise ProjectionError("max_points must be > 0")
    ordered_decisions = sorted(decisions, key=lambda item: getattr(item, "ts"))
    ordered_executions = sorted(executions, key=lambda item: getattr(item, "ts"))
    source_total = len(ordered_decisions) + len(ordered_executions)
    truncated = source_total > int(max_points)
    if truncated:
        ordered_decisions = ordered_decisions[-int(max_points):]
        ordered_executions = ordered_executions[-int(max_points):]
    decision_views = tuple(
        DecisionOverlay(
            ts=int(getattr(item, "ts")),
            side=str(getattr(item, "side") or ""),
            action=str(getattr(getattr(item, "action"), "value", getattr(item, "action"))),
            price=Fact.of(getattr(item, "price", None)),
            quantity=Fact.of(getattr(item, "quantity", None)),
            decision_id=Fact.of(getattr(item, "decision_id", None), unknown_reason="decision id not recorded"),
            reason=Fact.of(getattr(item, "reason", None), unknown_reason="decision carries no reason code"),
        )
        for item in ordered_decisions
    )
    execution_views = tuple(
        ExecutionOverlay(
            ts=int(getattr(item, "ts")),
            client_order_id=str(getattr(item, "client_order_id") or ""),
            event=str(getattr(item, "event") or type(item).__name__),
            detail=str(getattr(item, "detail") or ""),
        )
        for item in ordered_executions
    )
    return OverlayProjection(decisions=decision_views, executions=execution_views,
                             max_points=int(max_points), truncated=truncated)


# ---------------------------------------------------------------------- history buffer

@dataclass(slots=True)
class BoundedMarketHistory:
    """**有界**只读展示缓冲（运行时把事实喂进来；不是 market truth，也不落盘）。"""

    capacity: int
    run_id: str | None = None
    _states: deque = field(default_factory=deque)
    _snapshots: deque = field(default_factory=deque)
    _trades: deque = field(default_factory=deque)
    _health: deque = field(default_factory=deque)
    _decisions: deque = field(default_factory=deque)
    _executions: deque = field(default_factory=deque)

    def __post_init__(self) -> None:
        if isinstance(self.capacity, bool) or not isinstance(self.capacity, int) or self.capacity <= 0:
            raise ProjectionError("BoundedMarketHistory.capacity must be a positive int")
        for name in ("_states", "_snapshots", "_trades", "_health", "_decisions", "_executions"):
            setattr(self, name, deque(getattr(self, name), maxlen=self.capacity))

    def feed_state(self, state: object) -> None:
        self._states.append(state)

    def feed_snapshot(self, snapshot: object) -> None:
        self._snapshots.append(snapshot)

    def feed_trade(self, trade: object) -> None:
        self._trades.append(trade)

    def feed_health(self, transition: object) -> None:
        self._health.append(transition)

    def feed_decision(self, decision: object) -> None:
        self._decisions.append(decision)

    def feed_execution(self, event: object) -> None:
        self._executions.append(event)

    @property
    def counts(self) -> dict[str, int]:
        return {"states": len(self._states), "snapshots": len(self._snapshots), "trades": len(self._trades),
                "health": len(self._health), "decisions": len(self._decisions),
                "executions": len(self._executions), "capacity": self.capacity}

    def states(self) -> tuple[object, ...]:
        return tuple(self._states)

    def snapshots(self) -> tuple[object, ...]:
        return tuple(self._snapshots)

    def trades(self) -> tuple[object, ...]:
        return tuple(self._trades)

    def health(self) -> tuple[object, ...]:
        return tuple(self._health)

    def decisions(self) -> tuple[object, ...]:
        return tuple(self._decisions)

    def executions(self) -> tuple[object, ...]:
        return tuple(self._executions)


# ---------------------------------------------------------------------- replay control

@dataclass(frozen=True, slots=True)
class ReplayCommand:
    """给 **REPLAY owner**（ReplaySource / runtime session）执行的控制命令。"""

    verb: str
    payload: dict[str, object] = field(default_factory=dict)


class ReplayVerb(Enum):
    PLAY = "play"
    PAUSE = "pause"
    STEP = "step"
    SPEED = "speed"
    SEEK = "seek"


@dataclass(slots=True)
class ReplayControl:
    """local replay session control：**只能作用于 REPLAY runtime**（SC-13）。

    它不推进时间、不产生市场事实：只是把意图变成命令，交给 REPLAY owner 执行
    （前端**不得**自己模拟时间，见提案 §3）。非 REPLAY 模式一律拒绝。
    """

    mode: RuntimeMode
    _paused: bool = True
    _speed: float = 1.0
    _position: int = 0
    _commands: list[ReplayCommand] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.mode is not RuntimeMode.REPLAY:
            raise ReplayControlError(
                f"replay control is only available for REPLAY runtimes, got {self.mode.value}"
            )

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def speed(self) -> float:
        return self._speed

    @property
    def position(self) -> int:
        return self._position

    def commands(self) -> tuple[ReplayCommand, ...]:
        return tuple(self._commands)

    def play(self) -> ReplayCommand:
        self._paused = False
        return self._emit(ReplayVerb.PLAY)

    def pause(self) -> ReplayCommand:
        self._paused = True
        return self._emit(ReplayVerb.PAUSE)

    def step(self, *, count: int = 1) -> ReplayCommand:
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise ProjectionError("step count must be a positive int")
        self._paused = True
        self._position += count
        return self._emit(ReplayVerb.STEP, count=count, position=self._position)

    def set_speed(self, speed: float) -> ReplayCommand:
        if speed not in ALLOWED_SPEEDS:
            raise ProjectionError(f"speed must be one of {ALLOWED_SPEEDS}, got {speed!r}")
        self._speed = float(speed)
        return self._emit(ReplayVerb.SPEED, speed=self._speed)

    def seek(self, *, ordinal: int | None = None, ts: Milliseconds | None = None) -> ReplayCommand:
        """第一版 seek = restart replay + fast-forward（由 REPLAY owner 执行，不在 UI 里模拟）。"""
        if (ordinal is None) == (ts is None):
            raise ProjectionError("seek requires exactly one of ordinal / ts")
        target: dict[str, object] = {"ordinal": int(ordinal)} if ordinal is not None else {"ts": int(ts)}
        self._position = int(ordinal) if ordinal is not None else self._position
        return self._emit(ReplayVerb.SEEK, **target)

    def _emit(self, verb: ReplayVerb, **payload: object) -> ReplayCommand:
        command = ReplayCommand(verb=verb.value, payload={**payload, "speed": self._speed,
                                                         "paused": self._paused})
        self._commands.append(command)
        return command


__all__ = [
    "ALLOWED_SPEEDS",
    "DEPTH_NOTE",
    "BoundedMarketHistory",
    "DecisionOverlay",
    "DepthCell",
    "DepthHeatmap",
    "ExecutionOverlay",
    "HealthProjection",
    "HealthSegment",
    "MarketProjectionConfig",
    "OverlayProjection",
    "ProjectionError",
    "ReplayCommand",
    "ReplayControl",
    "ReplayControlError",
    "ReplayVerb",
    "TradePrint",
    "TradeProjection",
    "project_depth",
    "project_health",
    "project_overlays",
    "project_trades",
]
