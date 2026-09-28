"""P0001.7 测试脚手架：可控的 MarketState / PredictionRecord / RiskSnapshot 与策略 fixture。

这里的所有数值都是**测试值**（用于验证公式与门控行为），不是业务参数；
生产数值必须由调用方注入（P0001.7 §0.3）。
"""

from __future__ import annotations

from dataclasses import replace

from market.events.types import Venue
from market.health.state import BookHealth
from market.state.quality import build_data_quality
from market.state.types import (
    FEATURE_SCHEMA_VERSION,
    DepthFeatures,
    MarketIdentity,
    MarketState,
    PriceFeatures,
    StateTime,
    UNAVAILABLE_TRADE_FEATURES,
    ReturnsFeatures,
    VolatilityFeatures,
)
from portfolio.position import Position
from prediction.schema.market_v1 import QUESTION_SCHEMA_VERSION, market_state_hash
from prediction.types import (
    FutureReturnDistribution,
    Prediction,
    PredictionMode,
    PredictionRecord,
)
from risk.types import RiskSnapshot
from strategy.maker.types import MakerPolicyConfig
from tests.support import BASE_TS, SYMBOL

#: 脚手架默认 horizon（与 config.prediction_horizon_ms 一致）。
DEFAULT_HORIZON_MS = 5_000
#: 脚手架默认 tick。
DEFAULT_TICK_SIZE = 0.01

_CONFIG_DEFAULTS: dict[str, object] = {
    "tick_size": DEFAULT_TICK_SIZE,
    "quantity_step": 0.01,
    "min_quote_size": 0.01,
    "base_size": 1.0,
    "max_back_ticks": 3,
    "minimum_edge_bps": 5.0,
    "prediction_horizon_ms": DEFAULT_HORIZON_MS,
    "adverse_selection_retreat": 0.3,
    "adverse_selection_block": 0.6,
    "imbalance_retreat": 0.5,
    "microprice_skew_retreat_bps": 5.0,
    "predicted_move_retreat": 0.5,
    "target_position": 0.0,
    "inventory_scale": 1.0,
    "inventory_size_strength": 0.5,
    "inventory_retreat_ticks_max": 2,
    "size_factor_min": 0.2,
    "size_factor_max": 1.5,
    "confidence_ref_low": 0.2,
    "confidence_ref_high": 0.8,
    "confidence_factor_min": 0.5,
    "risk_factor_min": 0.25,
    "price_move_ticks_replace": 2,
    "max_quote_age_ms": 5_000,
    "size_drift_tolerance": 0.25,
}


def maker_config(**overrides: object) -> MakerPolicyConfig:
    """构造测试用 Maker 配置（显式覆盖任一参数）。"""
    values = dict(_CONFIG_DEFAULTS)
    values.update(overrides)
    return MakerPolicyConfig(**values)  # type: ignore[arg-type]


def market_state(
    *,
    best_bid: float | None = 100.0,
    best_ask: float | None = 101.0,
    bid_size: float | None = 5.0,
    ask_size: float | None = 2.0,
    imbalance_5: float | None = None,
    microprice: float | None = None,
    tradeable: bool = True,
    timestamp: int = BASE_TS,
    symbol: str = SYMBOL,
) -> MarketState:
    """构造可控的 MarketState（默认：健康、无失衡、microprice == mid）。

    `microprice` 默认等于 mid（skew 为 0）；`imbalance_5` 默认由 size 派生。
    """
    mid = None if best_bid is None or best_ask is None else (best_bid + best_ask) / 2.0
    spread = None if best_bid is None or best_ask is None else best_ask - best_bid
    spread_bps = None if spread is None or mid is None or mid == 0.0 else spread / mid * 10_000.0
    if imbalance_5 is None and bid_size is not None and ask_size is not None and (bid_size + ask_size) > 0.0:
        imbalance_5 = (bid_size - ask_size) / (bid_size + ask_size)
    if microprice is None:
        microprice = mid
    quality = _quality(tradeable=tradeable)
    return MarketState(
        identity=MarketIdentity(venue=Venue.BINANCE, symbol=symbol),
        time=StateTime(as_of_exchange_ts=timestamp, as_of_receive_ts=timestamp, event_ordinal=0),
        quality=quality,
        price=_price_features(
            best_bid=best_bid,
            best_ask=best_ask,
            bid_size=bid_size,
            ask_size=ask_size,
            mid=mid,
            spread=spread,
            spread_bps=spread_bps,
            microprice=microprice,
        ),
        depth=_depth_features(bid_size=bid_size, ask_size=ask_size, imbalance_5=imbalance_5, vamp=microprice),
        flow=_unavailable_flow(),
        trade=UNAVAILABLE_TRADE_FEATURES,
        returns=_flat_returns(),
        volatility=_flat_volatility(),
        feature_schema_version=FEATURE_SCHEMA_VERSION,
    )


def _quality(*, tradeable: bool):
    return build_data_quality(
        book_health=BookHealth.HEALTHY if tradeable else BookHealth.STALE,
        book_age_ms=0 if tradeable else 60_000,
        sequence_contiguous=True,
        window_coverage_ms=300_000,
        history_window_ms=300_000,
        completeness=1.0,
        max_book_age_ms=None,
    )


def _price_features(**kwargs: object) -> PriceFeatures:
    return PriceFeatures(**kwargs)  # type: ignore[arg-type]


def _depth_features(*, bid_size: float | None, ask_size: float | None, imbalance_5: float | None, vamp: float | None):
    return DepthFeatures(
        bid_depth_1=bid_size,
        bid_depth_5=None if bid_size is None else bid_size * 3.0,
        bid_depth_10=None if bid_size is None else bid_size * 5.0,
        bid_depth_20=None if bid_size is None else bid_size * 8.0,
        ask_depth_1=ask_size,
        ask_depth_5=None if ask_size is None else ask_size * 3.0,
        ask_depth_10=None if ask_size is None else ask_size * 5.0,
        ask_depth_20=None if ask_size is None else ask_size * 8.0,
        l1_imbalance=imbalance_5,
        depth_imbalance_1=imbalance_5,
        depth_imbalance_5=imbalance_5,
        depth_imbalance_10=imbalance_5,
        depth_imbalance_20=imbalance_5,
        vamp=vamp,
    )


def _flat_returns() -> ReturnsFeatures:
    return ReturnsFeatures(
        return_1s=0.0,
        return_3s=0.0,
        return_5s=0.0,
        return_15s=0.0,
        return_30s=0.0,
        return_60s=0.0,
        return_300s=0.0,
    )


def _flat_volatility() -> VolatilityFeatures:
    return VolatilityFeatures(
        realized_volatility_5s=0.0,
        realized_volatility_15s=0.0,
        realized_volatility_30s=0.0,
        realized_volatility_60s=0.0,
    )


def _unavailable_flow():
    from market.state.types import FlowFeatures

    return FlowFeatures(
        event_ofi=None,
        ofi_1s=None,
        ofi_5s=None,
        ofi_15s=None,
        normalized_ofi_1s=None,
        normalized_ofi_5s=None,
        normalized_ofi_15s=None,
        book_update_count=0,
        bid_update_count=0,
        ask_update_count=0,
        level_additions=0,
        level_removals=0,
    )


def make_prediction(
    *,
    strong_down: float = 0.05,
    down: float = 0.15,
    flat: float = 0.4,
    up: float = 0.3,
    strong_up: float = 0.1,
    buy_adverse_selection: float = 0.1,
    sell_adverse_selection: float = 0.1,
    buy_fill_probability: float = 0.5,
    sell_fill_probability: float = 0.5,
    derived_confidence: float = 0.9,
    provider_confidence: float | None = 0.9,
    horizon_ms: int = DEFAULT_HORIZON_MS,
) -> Prediction:
    """构造测试用 Prediction。"""
    return Prediction(
        future_return=(
            FutureReturnDistribution(
                horizon_ms=horizon_ms,
                strong_down=strong_down,
                down=down,
                flat=flat,
                up=up,
                strong_up=strong_up,
            ),
        ),
        buy_adverse_selection=buy_adverse_selection,
        sell_adverse_selection=sell_adverse_selection,
        buy_fill_probability=buy_fill_probability,
        sell_fill_probability=sell_fill_probability,
        provider_confidence=provider_confidence,
        derived_confidence=derived_confidence,
    )


def make_record(
    *,
    state: MarketState | None = None,
    prediction: Prediction | None = None,
    now_ms: int = BASE_TS,
    ttl_ms: int = 10_000,
    market_hash: str | None = None,
) -> PredictionRecord:
    """构造测试用 PredictionRecord（默认未过期）。"""
    resolved_state = state if state is not None else market_state()
    return PredictionRecord(
        request_id="req-1",
        sequence=1,
        market_state_hash=market_hash if market_hash is not None else market_state_hash(resolved_state),
        feature_schema_version=FEATURE_SCHEMA_VERSION,
        question_schema_version=QUESTION_SCHEMA_VERSION,
        provider="stub",
        model="jev-1.13",
        mode=PredictionMode.RECORDED,
        as_of=now_ms,
        request_created_at=now_ms,
        response_received_at=now_ms,
        latency_ms=0,
        expires_at=now_ms + ttl_ms,
        raw_response="{}",
        prediction=prediction if prediction is not None else make_prediction(),
    )


def position_for(position_qty: float, *, symbol: str = SYMBOL, mark: float = 100.0) -> Position:
    """构造与 `position_qty` 一致的 Position。"""
    return Position(
        symbol=symbol,
        qty=position_qty,
        avg_entry_price=0.0 if position_qty == 0.0 else mark,
        mark_price=mark,
    )


def maker_snapshot(
    *,
    position_qty: float = 0.0,
    now_ms: int = BASE_TS,
    mark_price: float | None = 100.0,
    balance: float = 10_000.0,
    open_order_exposure: float = 0.0,
    unresolved_order_count: int = 0,
    symbol: str = SYMBOL,
) -> RiskSnapshot:
    """构造测试用 RiskSnapshot（字段自洽）。"""
    position_notional = None if mark_price is None else abs(position_qty) * mark_price
    return RiskSnapshot(
        symbol=symbol,
        now_ms=now_ms,
        balance=balance,
        equity=balance,
        position_qty=position_qty,
        position_notional=position_notional,
        gross_exposure=position_notional,
        net_exposure=None if mark_price is None else position_qty * mark_price,
        open_order_exposure=open_order_exposure,
        available_balance=balance - open_order_exposure,
        realized_pnl_today=0.0,
        unrealized_pnl=0.0,
        drawdown=0.0,
        mark_price=mark_price,
        confirmed_open_exposure=open_order_exposure,
        uncertain_exposure=0.0,
        unresolved_order_count=unresolved_order_count,
    )


def with_exposure(snapshot: RiskSnapshot, *, unresolved_order_count: int = 0) -> RiskSnapshot:
    """复制快照并调整不确定订单数量（P0001.6.1 交互用）。"""
    return replace(snapshot, unresolved_order_count=unresolved_order_count)


__all__ = [
    "DEFAULT_HORIZON_MS",
    "DEFAULT_TICK_SIZE",
    "make_prediction",
    "make_record",
    "maker_config",
    "maker_snapshot",
    "market_state",
    "position_for",
    "with_exposure",
]
