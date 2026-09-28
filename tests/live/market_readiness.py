"""Public market-data → readiness evidence 的 **harness 侧**采集（P0001.9.5 §3）。

契约与纯映射已提升到产品侧 `market.readiness`；本模块只负责：

1. 从真实 public runtime 读取事实（telemetry / `FeatureEngine.book_health` / 历史 state / market generation）；
2. 用 `window_start()` 记录窗口基线，只统计**本窗口内**的 gap/resync/malformed；
3. 调用产品侧 `market.build_market_evidence(...)` 得到 `MarketReadinessEvidence`。

`wait_for_healthy()` 是**测量纪律**（不是 gate 放宽）：初始快照 + 锚定本身会推进 generation，
窗口起点必须放在锚定完成之后，否则会把"启动对齐"误报成"深度连续性问题"。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from market.events.types import Milliseconds
from market.health.state import BookHealth
from market.readiness import MarketReadinessEvidence, MarketReadinessPolicy, build_market_evidence, market_ready

from connectors.binance.market_data.runtime import LiveMarketDataRuntime

__all__ = [
    "MarketReadinessEvidence",
    "MarketReadinessPolicy",
    "collect_market_evidence",
    "market_evidence_report",
    "market_ready",
    "wait_for_healthy",
    "window_start",
]


@dataclass(frozen=True, slots=True)
class MarketWindowStart:
    """窗口起点基线：只看**本窗口内**的增量。"""

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
    """pump 直到盘口首次 HEALTHY（初始快照 + 锚定完成），返回是否达成。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if runtime.engine.book_health is BookHealth.HEALTHY:
            return True
        runtime.pump_once(timeout_s=0.5, max_messages=32)
    return runtime.engine.book_health is BookHealth.HEALTHY


def collect_market_evidence(
    runtime: LiveMarketDataRuntime,
    *,
    baseline: MarketWindowStart,
    now_ms: Milliseconds,
    policy: MarketReadinessPolicy,
) -> MarketReadinessEvidence:
    """读取本窗口内的真实 public 事实并交给产品侧契约判定。"""
    telemetry = runtime.telemetry
    history = runtime.history
    feed_age_ms = None if not history else max(0, now_ms - history[-1].time.as_of_receive_ts)
    return build_market_evidence(
        observed_at=now_ms,
        generation=runtime.market_generation,
        book_health=runtime.engine.book_health.value,
        anchored=bool(history),
        mark_age_ms=telemetry.mark_age_ms,
        feed_age_ms=feed_age_ms,
        depth_gap_count=telemetry.depth_gap_count - baseline.depth_gap_count,
        resync_count=telemetry.resync_count - baseline.resync_count,
        malformed_count=telemetry.malformed_message_count - baseline.malformed_message_count,
        agg_trade_count=telemetry.agg_trade_count - baseline.agg_trade_count,
        policy=policy,
    )


def market_evidence_report(evidence: MarketReadinessEvidence) -> dict:
    """报告片段（无凭据、纯事实）。"""
    return {
        "ready": evidence.ready,
        "problems": list(evidence.problems),
        "generation": evidence.generation,
        "observed_at": evidence.observed_at,
        "book_health": evidence.book_health,
        "anchored": evidence.anchored,
        "depth_gap_count": evidence.depth_gap_count,
        "resync_count": evidence.resync_count,
        "malformed_count": evidence.malformed_count,
        "agg_trade_count": evidence.agg_trade_count,
        "mark_age_ms": evidence.mark_age_ms,
        "feed_age_ms": evidence.feed_age_ms,
    }
