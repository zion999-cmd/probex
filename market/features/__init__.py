"""市场特征层：由盘口事实计算 feature 段。

本层只依赖 `market/events`、`market/book`、`market/health`、`market/state` 与标准库，
不 import Jev / Strategy / Execution / Portfolio / Risk。
"""

from __future__ import annotations

from market.features.depth import DEPTH_LEVELS, VAMP_LEVELS, compute_depth_features, imbalance, vamp
from market.features.engine import FEATURE_VIEW_DEPTH, FeatureEngine
from market.features.flow import OFI_WINDOWS_MS, FlowFeaturesCalculator, mutation_ofi
from market.features.price import compute_price_features, microprice
from market.features.returns import (
    HISTORY_WINDOW_MS,
    PRICE_HISTORY_HORIZON_MS,
    RETURN_WINDOWS_MS,
    UNAVAILABLE_RETURNS,
    compute_returns,
    log_returns,
)
from market.features.volatility import (
    UNAVAILABLE_VOLATILITY,
    VOLATILITY_WINDOWS_MS,
    compute_volatility,
    realized_volatility,
)
from market.features.windows import EventWindow, TimeSeries, TimeWindow

__all__ = [
    "DEPTH_LEVELS",
    "FEATURE_VIEW_DEPTH",
    "HISTORY_WINDOW_MS",
    "OFI_WINDOWS_MS",
    "PRICE_HISTORY_HORIZON_MS",
    "RETURN_WINDOWS_MS",
    "UNAVAILABLE_RETURNS",
    "UNAVAILABLE_VOLATILITY",
    "VAMP_LEVELS",
    "VOLATILITY_WINDOWS_MS",
    "EventWindow",
    "FeatureEngine",
    "FlowFeaturesCalculator",
    "TimeSeries",
    "TimeWindow",
    "compute_depth_features",
    "compute_price_features",
    "compute_returns",
    "compute_volatility",
    "imbalance",
    "log_returns",
    "microprice",
    "mutation_ofi",
    "realized_volatility",
    "vamp",
]
