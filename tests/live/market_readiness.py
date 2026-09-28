"""Public market-data → readiness evidence 的 **harness 侧**映射（P0001.9.4.1.1 §1）。

为什么放在 `tests/live/`：本阶段只做集成验证，**不**动 D-043 的长期架构问题
（`market_ready` 目前仍是 readiness 的裸输入）。因此这里把真实 public runtime 的可观测事实
映射成一个 `market_ready` 判定 + 完整证据字典，供 harness 使用；产品模块不改。

判定用的全部是**真实事实**（不硬编码）：

| 证据 | 来源 |
| --- | --- |
| 盘口可信度 | `FeatureEngine.book_health`（`HEALTHY`） |
| 快照边界 | `telemetry.snapshot_count > 0` 且窗口内无 `snapshot_failure_count` |
| depth 连续性 | 窗口内 `depth_gap_count == 0` 且 `resync_count == 0` |
| mark 新鲜度 | `telemetry.mark_age_ms <= max_mark_age_ms` |
| feed 新鲜度 | `now - history[-1].time.as_of_receive_ts <= max_feed_age_ms` |
| 成交出现 | 窗口内 `agg_trade_count > 0` |
| 帧完整性 | 窗口内 `malformed_message_count == 0` |
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from market.events.types import Milliseconds
from market.health.state import BookHealth

from connectors.binance.market_data.runtime import LiveMarketDataRuntime


@dataclass(frozen=True, slots=True)
class MarketWindowStart:
    """窗口起点基线：只看**本窗口内**的增量，避免把历史 resync 算进来。"""

    snapshot_count: int
    snapshot_failure_count: int
    resync_count: int
    depth_gap_count: int
    malformed_message_count: int
    agg_trade_count: int


def window_start(runtime: LiveMarketDataRuntime) -> MarketWindowStart:
    telemetry = runtime.telemetry
    return MarketWindowStart(
        snapshot_count=telemetry.snapshot_count,
        snapshot_failure_count=telemetry.snapshot_failure_count,
        resync_count=telemetry.resync_count,
        depth_gap_count=telemetry.depth_gap_count,
        malformed_message_count=telemetry.malformed_message_count,
        agg_trade_count=telemetry.agg_trade_count,
    )


def wait_for_healthy(runtime: LiveMarketDataRuntime, *, timeout_s: float) -> bool:
    """pump 直到盘口首次 HEALTHY（初始快照 + 锚定完成），返回是否达成。

    这是**测量纪律**，不是 gate 放宽：初始锚定本身会消耗一次 snapshot/可能的 resync，
    若把窗口起点放在它之前，会把"启动对齐"误报成"深度连续性问题"。
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if runtime.engine.book_health is BookHealth.HEALTHY:
            return True
        runtime.pump_once(timeout_s=0.5, max_messages=32)
    return runtime.engine.book_health is BookHealth.HEALTHY


@dataclass(frozen=True, slots=True)
class MarketReadinessEvidence:
    """真实 public 事实（不判定，只是事实）。"""

    book_health: str
    #: 绝对快照数（>= 1 且 `anchor_established` ⇒ 已建立可信盘口；窗口内增量必然是 0，因为基线在锚定之后）
    snapshot_total: int
    #: 是否已完成初始锚定（`wait_for_healthy` 达成）
    anchor_established: bool
    snapshot_count: int
    resync_count: int
    depth_gap_count: int
    malformed_message_count: int
    agg_trade_count: int
    mark_age_ms: Milliseconds | None
    feed_age_ms: Milliseconds | None
    event_lag_ms: int | None
    state_count: int
    last_error: str | None


def collect_market_evidence(
    runtime: LiveMarketDataRuntime, *, baseline: MarketWindowStart, now_ms: Milliseconds
) -> MarketReadinessEvidence:
    """读取本窗口内的真实 public 事实。"""
    telemetry = runtime.telemetry
    history = runtime.history
    feed_age_ms = None
    if history:
        feed_age_ms = max(0, now_ms - history[-1].time.as_of_receive_ts)
    return MarketReadinessEvidence(
        book_health=runtime.engine.book_health.value,
        snapshot_total=telemetry.snapshot_count,
        anchor_established=bool(history),
        snapshot_count=telemetry.snapshot_count - baseline.snapshot_count,
        resync_count=telemetry.resync_count - baseline.resync_count,
        depth_gap_count=telemetry.depth_gap_count - baseline.depth_gap_count,
        malformed_message_count=telemetry.malformed_message_count - baseline.malformed_message_count,
        agg_trade_count=telemetry.agg_trade_count - baseline.agg_trade_count,
        mark_age_ms=telemetry.mark_age_ms,
        feed_age_ms=feed_age_ms,
        event_lag_ms=telemetry.event_lag_ms,
        state_count=telemetry.state_count,
        last_error=telemetry.last_error,
    )


@dataclass(frozen=True, slots=True)
class MarketReadinessPolicy:
    """harness 判定阈值（**必须显式给出**；产品 readiness policy 目前不含这些字段）。"""

    max_mark_age_ms: Milliseconds
    max_feed_age_ms: Milliseconds

    def __post_init__(self) -> None:
        for name in ("max_mark_age_ms", "max_feed_age_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"MarketReadinessPolicy.{name} must be a positive int")


def market_ready(
    evidence: MarketReadinessEvidence, *, policy: MarketReadinessPolicy
) -> tuple[bool, tuple[str, ...]]:
    """把真实事实映射成 `market_ready` + 未满足原因（**不硬编码 true**）。"""
    problems: list[str] = []
    if evidence.book_health != BookHealth.HEALTHY.value:
        problems.append(f"book_health={evidence.book_health}")
    if evidence.snapshot_total <= 0 or not evidence.anchor_established:
        problems.append(
            f"snapshot anchor not established (snapshot_total={evidence.snapshot_total}, "
            f"anchor_established={evidence.anchor_established})"
        )
    if evidence.resync_count > 0:
        problems.append(f"resync_count={evidence.resync_count} in window")
    if evidence.depth_gap_count > 0:
        problems.append(f"depth_gap_count={evidence.depth_gap_count} in window")
    if evidence.malformed_message_count > 0:
        problems.append(f"malformed_message_count={evidence.malformed_message_count} in window")
    if evidence.agg_trade_count <= 0:
        problems.append("no aggTrade observed in window")
    if evidence.mark_age_ms is None:
        problems.append("mark price missing")
    elif evidence.mark_age_ms > policy.max_mark_age_ms:
        problems.append(f"mark_age_ms={evidence.mark_age_ms} > {policy.max_mark_age_ms}")
    if evidence.feed_age_ms is None:
        problems.append("no market state produced yet")
    elif evidence.feed_age_ms > policy.max_feed_age_ms:
        problems.append(f"feed_age_ms={evidence.feed_age_ms} > {policy.max_feed_age_ms}")
    return (not problems), tuple(problems)


def market_evidence_report(evidence: MarketReadinessEvidence, *, ready: bool, problems: tuple[str, ...]) -> dict:
    """报告片段（无凭据、纯事实）。"""
    return {
        "ready": ready,
        "problems": list(problems),
        "book_health": evidence.book_health,
        "snapshot_total": evidence.snapshot_total,
        "anchor_established": evidence.anchor_established,
        "snapshot_count_in_window": evidence.snapshot_count,
        "resync_count": evidence.resync_count,
        "depth_gap_count": evidence.depth_gap_count,
        "malformed_message_count": evidence.malformed_message_count,
        "agg_trade_count": evidence.agg_trade_count,
        "mark_age_ms": evidence.mark_age_ms,
        "feed_age_ms": evidence.feed_age_ms,
        "event_lag_ms": evidence.event_lag_ms,
        "state_count": evidence.state_count,
        "last_error": evidence.last_error,
    }


__all__ = [
    "MarketReadinessEvidence",
    "MarketReadinessPolicy",
    "MarketWindowStart",
    "collect_market_evidence",
    "market_evidence_report",
    "market_ready",
    "window_start",
]
