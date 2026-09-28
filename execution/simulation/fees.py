"""FeeSchedule：模拟成交的手续费来源（P0001.8 §12）。

纪律：

- **不从历史数据猜手续费**，也不内置生产默认值：`maker_fee_rate` 必须由调用方显式注入；
- 第一版只支持做市（maker）费率，非负且有限（返佣 / 多资产换算属后续阶段）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from execution.simulation.types import SimulationError


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """显式费率表。"""

    #: maker 费率（比例，例如 0.0002 = 2 bps）。无默认值。
    maker_fee_rate: float
    #: 手续费资产（必须与账户结算资产一致，不做换算）。
    fee_asset: str

    def __post_init__(self) -> None:
        rate = self.maker_fee_rate
        if isinstance(rate, bool) or not isinstance(rate, (int, float)):
            raise SimulationError("FeeSchedule.maker_fee_rate must be a number")
        value = float(rate)
        if not math.isfinite(value) or value < 0.0:
            raise SimulationError(f"FeeSchedule.maker_fee_rate must be a non-negative finite number, got {rate!r}")
        object.__setattr__(self, "maker_fee_rate", value)
        if not isinstance(self.fee_asset, str) or not self.fee_asset:
            raise SimulationError("FeeSchedule.fee_asset must be a non-empty string")

    def maker_fee(self, *, price: float, quantity: float) -> float:
        """做市成交手续费（正数 = 支出）。"""
        for field, number in (("price", price), ("quantity", quantity)):
            if isinstance(number, bool) or not isinstance(number, (int, float)):
                raise SimulationError(f"maker_fee({field}) must be a number")
            value = float(number)
            if not math.isfinite(value) or value <= 0.0:
                raise SimulationError(f"maker_fee({field}) must be a positive finite number, got {number!r}")
        return float(price) * float(quantity) * self.maker_fee_rate


__all__ = ["FeeSchedule"]
