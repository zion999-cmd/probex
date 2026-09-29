"""`SystemSnapshot`：一个运行实例当前的完整产品视图（P0001.10 SC-1 / SC-2）。

REPLAY / PAPER / TESTNET / LIVE **共用同一 schema**：差异只体现在
`runtime.mode` / `runtime.environment` 与各字段的 known/unknown，而不是不同的响应形状。
"""

from __future__ import annotations

from dataclasses import dataclass

from market.events.types import Milliseconds

from product.types import (
    SCHEMA_VERSION,
    BlockerView,
    ConfigView,
    EvidenceView,
    ExecutionSafetyView,
    ExecutionView,
    HealthView,
    MarketView,
    PortfolioView,
    PredictionView,
    ReadinessView,
    RiskView,
    RuntimeIdentity,
    StrategyView,
)


@dataclass(frozen=True, slots=True)
class SystemSnapshot:
    generated_at: Milliseconds
    runtime: RuntimeIdentity
    market: MarketView
    prediction: PredictionView
    strategy: StrategyView
    risk: RiskView
    execution: ExecutionView
    portfolio: PortfolioView
    readiness: ReadinessView
    health: HealthView
    evidence: EvidenceView
    config: ConfigView
    execution_safety: ExecutionSafetyView = ExecutionSafetyView()
    blockers: tuple[BlockerView, ...] = ()
    schema_version: str = SCHEMA_VERSION
