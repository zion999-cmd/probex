"""市场状态层：不可变 MarketState 与其质量闸门。"""

from __future__ import annotations

from market.state.builder import build_market_state, compute_completeness
from market.state.quality import DataQuality, build_data_quality
from market.state.types import (
    FEATURE_SCHEMA_VERSION,
    DepthFeatures,
    FlowFeatures,
    MarketIdentity,
    MarketState,
    PriceFeatures,
    ReturnsFeatures,
    StateTime,
    TradeFeatures,
    VolatilityFeatures,
)

__all__ = [
    "FEATURE_SCHEMA_VERSION",
    "DataQuality",
    "DepthFeatures",
    "FlowFeatures",
    "MarketIdentity",
    "MarketState",
    "PriceFeatures",
    "ReturnsFeatures",
    "StateTime",
    "TradeFeatures",
    "VolatilityFeatures",
    "build_data_quality",
    "build_market_state",
    "compute_completeness",
]
