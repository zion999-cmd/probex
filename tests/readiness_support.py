"""readiness 测试脚手架：构造**已确认**的 durable high-watermark 证据（P0001.9.4.2）。

只用内存 store，不落盘；值都是测试值。
"""

from __future__ import annotations

from dataclasses import replace

from market.readiness import MarketReadinessEvidence
from readiness.types import (
    Environment,
    EnvironmentEvidence,
    EnvironmentValidationEvidence,
    EnvironmentValidationStatus,
)
from risk.high_watermark import (
    ActivationPreconditions,
    EquityHighWatermarkState,
    HighWatermarkEvidence,
    HighWatermarkScope,
    HighWatermarkStatus,
    HighWatermarkTracker,
)
from tests.support import BASE_TS


class InMemoryHighWatermarkStore:
    """内存 store（实现 `save`/`load`；供测试用）。"""

    def __init__(self, state: EquityHighWatermarkState | None = None) -> None:
        self._state = state
        self.save_count = 0

    def load(self) -> EquityHighWatermarkState | None:
        return self._state

    def save(self, state: EquityHighWatermarkState) -> None:
        self._state = state
        self.save_count += 1


class FailingHighWatermarkStore:
    """总是写失败的 store（用于 SC-7：持久化失败必须 fail closed）。"""

    def save(self, state: EquityHighWatermarkState) -> None:
        raise RuntimeError("injected durable store failure")

    def load(self) -> EquityHighWatermarkState | None:
        return None


def satisfied_preconditions(**overrides: object) -> ActivationPreconditions:
    values: dict[str, object] = {
        "recovery_recovered": True,
        "position_flat": True,
        "open_orders_zero": True,
        "unresolved_orders_zero": True,
        "daily_pnl_known": True,
        "current_equity_known": True,
        "account_snapshot_fresh": True,
        "equity_consistent": True,
        "scope": HighWatermarkScope.TESTNET,
        "deployment_id": "testnet-deployment",
    }
    values.update(overrides)
    return ActivationPreconditions(**values)  # type: ignore[arg-type]


def market_evidence(*, ready: bool = True, generation: int = 7, observed_at: int = BASE_TS, **overrides: object):
    """测试用 market evidence（`ready` 与 `problems` 自洽）。"""
    values: dict[str, object] = {
        "ready": ready,
        "observed_at": observed_at,
        "generation": generation,
        "book_health": "healthy" if ready else "stale",
        "anchored": True,
        "mark_age_ms": 100,
        "feed_age_ms": 100,
        "depth_gap_count": 0,
        "resync_count": 0,
        "malformed_count": 0,
        "agg_trade_count": 50,
        "problems": () if ready else ("book_health=stale",),
    }
    values.update(overrides)
    return MarketReadinessEvidence(**values)  # type: ignore[arg-type]


def environment_evidence(*, environment: Environment = Environment.TESTNET, validated: bool = False,
                         validated_at: int = BASE_TS, validation_id: str = "validation-1",
                         account_scope: str = "binance-usdm-mainnet-account",
                         evidence_source: str = "live-readiness-runner"):
    """测试用 environment evidence（默认 NOT_VALIDATED ⇒ 主网仍 BLOCKED，SC-23）。"""
    if not validated:
        return EnvironmentEvidence.not_validated(environment=environment)
    return EnvironmentEvidence(
        environment=environment,
        validation=EnvironmentValidationEvidence(
            environment=environment,
            validation_status=EnvironmentValidationStatus.VALIDATED,
            validated_at=validated_at,
            validation_id=validation_id,
            account_scope=account_scope,
            evidence_source=evidence_source,
        ),
    )


def activated_state(*, equity: float = 1_000.0, ts: int = BASE_TS, activation_id: str = "act-1") -> EquityHighWatermarkState:
    """直接构造一个 ACTIVE 状态（不经过 tracker，单测更直观）。"""
    return EquityHighWatermarkState(
        scope=HighWatermarkScope.TESTNET,
        deployment_id="testnet-deployment",
        activation_id=activation_id,
        activation_ts=ts,
        activation_equity=equity,
        peak_equity=equity,
        peak_ts=ts,
        last_equity=equity,
        last_observed_ts=ts,
        capital_flow_checked_through=ts,
        generation=1,
        status=HighWatermarkStatus.ACTIVE,
    )


def active_hwm(*, equity: float = 1_000.0, ts: int = BASE_TS) -> HighWatermarkEvidence:
    return HighWatermarkEvidence.from_state(activated_state(equity=equity, ts=ts))


def activated_tracker(*, equity: float = 1_000.0, ts: int = BASE_TS):
    """返回 (tracker, store)，已 activate 到给定 equity。"""
    store = InMemoryHighWatermarkStore()
    tracker = HighWatermarkTracker(state=None, store=store)
    tracker.activate(
        preconditions=satisfied_preconditions(), current_equity=equity, activation_id="act-1", ts=ts
    )
    return tracker, store


def with_peak(tracker: HighWatermarkTracker, *, peak: float, ts: int) -> HighWatermarkEvidence:
    """把 tracker 推进到给定 peak（若高于当前）并返回证据。"""
    tracker.observe_equity(current_equity=peak, ts=ts)
    return tracker.evidence()


__all__ = [
    "FailingHighWatermarkStore",
    "InMemoryHighWatermarkStore",
    "activated_state",
    "activated_tracker",
    "active_hwm",
    "environment_evidence",
    "market_evidence",
    "satisfied_preconditions",
    "with_peak",
]
