"""MarketState 组装。

这是唯一写入 `feature_schema_version` 的地方；feature 段本身由
`market/features` 计算，本模块不再重新解释任何公式。
"""

from __future__ import annotations

from dataclasses import fields

from market.state.quality import DataQuality
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

#: `completeness` 统计的 feature 段（不含 trade：trade 可用性由
#: `trade_stream_available` 单独表达，否则无成交数据时 completeness 永远无法达到 1）。
_COMPLETENESS_SECTIONS = (PriceFeatures, DepthFeatures, FlowFeatures, ReturnsFeatures, VolatilityFeatures)


def compute_completeness(
    *,
    price: PriceFeatures,
    depth: DepthFeatures,
    flow: FlowFeatures,
    returns: ReturnsFeatures,
    volatility: VolatilityFeatures,
) -> float:
    """book 相关 feature 段的可用比例：非 None 字段数 / 字段总数。"""
    total = 0
    available = 0
    for section in (price, depth, flow, returns, volatility):
        for field in fields(section):
            total += 1
            if getattr(section, field.name) is not None:
                available += 1
    return available / total


def build_market_state(
    *,
    identity: MarketIdentity,
    time: StateTime,
    quality: DataQuality,
    price: PriceFeatures,
    depth: DepthFeatures,
    flow: FlowFeatures,
    trade: TradeFeatures,
    returns: ReturnsFeatures,
    volatility: VolatilityFeatures,
) -> MarketState:
    """装配 `MarketState`。"""
    return MarketState(
        identity=identity,
        time=time,
        quality=quality,
        price=price,
        depth=depth,
        flow=flow,
        trade=trade,
        returns=returns,
        volatility=volatility,
        feature_schema_version=FEATURE_SCHEMA_VERSION,
    )


__all__ = [
    "FEATURE_SCHEMA_VERSION",
    "build_market_state",
    "compute_completeness",
    "_COMPLETENESS_SECTIONS",
]
