"""InventoryBias：按净持仓调整双边报价倾向（P0001.7 §0.2 / §0.7）。

第一版是**市场中性**库存控制：`target_position` 由调用方显式给出（通常为 0）。
净多头（`qty > target`）时：

- 买侧更保守：缩量 + 多退 tick（减少继续加仓）；
- 卖侧更积极：放量 + 不退 tick（且该侧在 policy 层被标记为 reduce-only）。

净空头对称。本模块不做 Avellaneda-Stoikov，也不绕过 RiskGate。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from strategy.maker.types import MakerPolicyConfig


@dataclass(frozen=True, slots=True)
class InventoryBias:
    """库存偏置结果。所有 factor 已在配置边界内被 clamp。"""

    #: `(qty - target) / inventory_scale`，clamp 到 [-1, 1]。
    normalized_inventory: float
    buy_size_factor: float
    sell_size_factor: float
    #: 净多头时买侧额外后退的 tick 数（≥ 0）。
    buy_extra_retreat_ticks: int
    #: 净空头时卖侧额外后退的 tick 数（≥ 0）。
    sell_extra_retreat_ticks: int
    detail: str


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def compute_inventory_bias(*, position_qty: float, config: MakerPolicyConfig) -> InventoryBias:
    """由当前净持仓与 `target_position` 派生库存偏置（纯函数，确定性）。"""
    deviation = float(position_qty) - config.target_position
    normalized = _clamp(deviation / config.inventory_scale, -1.0, 1.0)
    strength = config.inventory_size_strength
    buy_factor = _clamp(1.0 - normalized * strength, config.size_factor_min, config.size_factor_max)
    sell_factor = _clamp(1.0 + normalized * strength, config.size_factor_min, config.size_factor_max)

    max_ticks = config.inventory_retreat_ticks_max
    magnitude = int(math.floor(abs(normalized) * max_ticks + 0.5))
    buy_ticks = magnitude if normalized > 0.0 else 0
    sell_ticks = magnitude if normalized < 0.0 else 0

    detail = (
        f"inventory={position_qty:.10g} target={config.target_position:.10g} "
        f"normalized={normalized:.6f} size_factors=({buy_factor:.6f},{sell_factor:.6f}) "
        f"retreat_ticks=({buy_ticks},{sell_ticks})"
    )
    return InventoryBias(
        normalized_inventory=normalized,
        buy_size_factor=buy_factor,
        sell_size_factor=sell_factor,
        buy_extra_retreat_ticks=buy_ticks,
        sell_extra_retreat_ticks=sell_ticks,
        detail=detail,
    )


__all__ = ["InventoryBias", "compute_inventory_bias"]
