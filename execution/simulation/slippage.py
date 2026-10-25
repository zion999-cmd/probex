"""固定 bps 滑点模型（人类 2026-10-24 授权批次4）。

只做一个**显式、确定性**的价格调整（不做深度冲击/部分成交建模）：

    taker buy  成交价 = best_ask × (1 + slippage_bps / 10_000)
    taker sell 成交价 = best_bid × (1 − slippage_bps / 10_000)

- bps 必须显式给出（无生产默认值），有限且非负；
- 参考价必须为正数（盘口未知 ⇒ 调用方 fail-closed，不在此模块猜测）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from portfolio.types import Side


class SlippageError(ValueError):
    """滑点模型契约错误。"""


@dataclass(frozen=True, slots=True)
class FixedBpsSlippage:
    """吃单价相对盘口的固定基点调整（显式配置，不代表真实滑点为零或仅此值）。"""

    slippage_bps: float

    def __post_init__(self) -> None:
        if isinstance(self.slippage_bps, bool) or \
                not isinstance(self.slippage_bps, (int, float)):
            raise SlippageError("slippage_bps must be a number")
        value = float(self.slippage_bps)
        if not math.isfinite(value) or value < 0.0:
            raise SlippageError(f"slippage_bps must be a non-negative finite number, got {value!r}")
        object.__setattr__(self, "slippage_bps", value)

    def exec_price(self, *, side: Side, reference_price: float) -> float:
        if not isinstance(reference_price, (int, float)) or isinstance(reference_price, bool):
            raise SlippageError("reference_price must be a number")
        ref = float(reference_price)
        if not math.isfinite(ref) or ref <= 0.0:
            raise SlippageError(f"reference_price must be positive, got {reference_price!r}")
        factor = 1.0 + self.slippage_bps / 10_000.0
        if side is Side.SELL:
            return ref * (2.0 - factor)  # ref × (1 − bps/1e4)
        return ref * factor


__all__ = ["FixedBpsSlippage", "SlippageError"]
