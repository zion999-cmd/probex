"""从 deployment config 构造既有 `MakerPolicyConfig`（P0001.14 §3/§12）。

**不发明任何业务数值**：`strategy.maker.*` 缺任一必填键 ⇒ 返回 `None`（⇒ runtime 不产生 decision，
trace 记为 ABSENT，UI 显示 "no maker decision"）；数值由操作者/profile 显式给出。
"""

from __future__ import annotations

from typing import Any

#: (config key, MakerPolicyConfig field, coercion)
MAKER_KEYS: tuple[tuple[str, str, type], ...] = (
    ("strategy.maker.tick_size", "tick_size", float),
    ("strategy.maker.quantity_step", "quantity_step", float),
    ("strategy.maker.min_quote_size", "min_quote_size", float),
    ("strategy.maker.base_size", "base_size", float),
    ("strategy.maker.max_back_ticks", "max_back_ticks", int),
    ("strategy.maker.minimum_edge_bps", "minimum_edge_bps", float),
    ("strategy.maker.prediction_horizon_ms", "prediction_horizon_ms", int),
    ("strategy.maker.adverse_selection_retreat", "adverse_selection_retreat", float),
    ("strategy.maker.adverse_selection_block", "adverse_selection_block", float),
    ("strategy.maker.imbalance_retreat", "imbalance_retreat", float),
    ("strategy.maker.microprice_skew_retreat_bps", "microprice_skew_retreat_bps", float),
    ("strategy.maker.predicted_move_retreat", "predicted_move_retreat", float),
    ("strategy.maker.target_position", "target_position", float),
    ("strategy.maker.inventory_scale", "inventory_scale", float),
    ("strategy.maker.inventory_size_strength", "inventory_size_strength", float),
    ("strategy.maker.inventory_retreat_ticks_max", "inventory_retreat_ticks_max", int),
    ("strategy.maker.size_factor_min", "size_factor_min", float),
    ("strategy.maker.size_factor_max", "size_factor_max", float),
    ("strategy.maker.confidence_ref_low", "confidence_ref_low", float),
    ("strategy.maker.confidence_ref_high", "confidence_ref_high", float),
    ("strategy.maker.confidence_factor_min", "confidence_factor_min", float),
    ("strategy.maker.risk_factor_min", "risk_factor_min", float),
    ("strategy.maker.price_move_ticks_replace", "price_move_ticks_replace", int),
    ("strategy.maker.max_quote_age_ms", "max_quote_age_ms", int),
    ("strategy.maker.size_drift_tolerance", "size_drift_tolerance", float),
)

#: 技术 trigger 参数（非业务阈值；缺省用 runtime 默认）
LOOP_KEYS: tuple[tuple[str, str], ...] = (
    ("runtime.loop.tick_ms", "tick_ms"),
    ("runtime.loop.decision_interval_ms", "decision_interval_ms"),
    ("runtime.loop.prediction_min_interval_ms", "prediction_min_interval_ms"),
)


def build_maker_policy(values: dict[str, Any]) -> object | None:
    """全部键齐备才构造 `MakerPolicy`；否则 None（不编造业务数值）。"""
    if any(key not in values for key, _, _ in MAKER_KEYS):
        return None
    from strategy.maker.policy import MakerPolicy
    from strategy.maker.types import MakerPolicyConfig

    kwargs: dict[str, Any] = {}
    for key, field, coerce in MAKER_KEYS:
        kwargs[field] = coerce(values[key])
    return MakerPolicy(MakerPolicyConfig(**kwargs))


def build_loop_options(values: dict[str, Any]) -> dict[str, int]:
    """loop 技术参数（可选覆盖；缺省留给 DecisionLoopConfig 的默认值）。"""
    options: dict[str, int] = {}
    for key, field in LOOP_KEYS:
        if key in values:
            options[field] = int(values[key])
    return options


__all__ = ["LOOP_KEYS", "MAKER_KEYS", "build_loop_options", "build_maker_policy"]
