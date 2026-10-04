"""`SystemSnapshot`：一个运行实例当前的完整产品视图（P0001.10 SC-1 / SC-2）。

REPLAY / PAPER / TESTNET / LIVE **共用同一 schema**：差异只体现在
`runtime.mode` / `runtime.environment` 与各字段的 known/unknown，而不是不同的响应形状。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from market.events.types import Milliseconds

from product.types import (
    SCHEMA_VERSION,
    BlockerView,
    ConfigView,
    UNKNOWN_INSTRUMENT_VIEW,
    UNKNOWN_MARKET_CONNECTOR_HEALTH,
    UNKNOWN_PRIVATE_CONNECTOR_HEALTH,
    UNKNOWN_REFERENCE_PRICE_VIEW,
    UNKNOWN_VENUE_VIEW,
    ConnectorHealth,
    EvidenceView,
    ExecutionSafetyView,
    ExecutionView,
    HealthView,
    InstrumentView,
    MarketView,
    OpsView,
    PortfolioView,
    PredictionView,
    ReadinessView,
    ReferencePriceView,
    RiskView,
    RuntimeIdentity,
    StrategyView,
    VenueView,
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
    #: P0001.15 §21–§26：instrument / venue / reference price / connector health（两个 connector 分开）
    instrument: InstrumentView = UNKNOWN_INSTRUMENT_VIEW
    venue: VenueView = UNKNOWN_VENUE_VIEW
    reference_price: ReferencePriceView = UNKNOWN_REFERENCE_PRICE_VIEW
    market_connector_health: ConnectorHealth = UNKNOWN_MARKET_CONNECTOR_HEALTH
    private_connector_health: ConnectorHealth = UNKNOWN_PRIVATE_CONNECTOR_HEALTH
    execution_safety: ExecutionSafetyView = ExecutionSafetyView()
    ops: OpsView = OpsView()
    blockers: tuple[BlockerView, ...] = ()
    schema_version: str = SCHEMA_VERSION
