"""Risk 限额配置（§10）。

约定：

- 所有限额缺省为 `None` = **未启用**；已配置但数据缺失时 RiskGate 一律 REJECT（fail closed）。
- `max_daily_loss` 为正数，表示当日净亏损（含手续费与资金费）的允许上限。
- `max_drawdown_pct` ∈ (0, 1]，按 `peak_equity` 的比例判定。
- `max_leverage` >= 1；未配置时可用余额检查按 `effective_leverage = 1.0`（保守：不允许杠杆）。
- `min_liquidation_distance_bps` 只用于**读取** `LiquidationInfo`，本阶段不自行推导强平价。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from market.events.types import Milliseconds
from risk.types import KillSwitchMode


def _require_positive(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number")
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"{field} must be a positive finite number, got {value!r}")
    return number


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """风险限额集合。"""

    max_position_qty: float | None = None
    max_position_notional: float | None = None
    max_open_order_exposure: float | None = None
    max_daily_loss: float | None = None
    max_drawdown_pct: float | None = None
    max_leverage: float | None = None
    min_liquidation_distance_bps: float | None = None
    max_mark_age_ms: Milliseconds | None = None
    #: 兼容位（P0001.5）：True 等价于 `kill_switch_mode = HALT_ALL`。
    kill_switch: bool = False
    #: 三态语义（P0001.6）：NORMAL / REDUCE_ONLY / HALT_ALL
    kill_switch_mode: KillSwitchMode = KillSwitchMode.NORMAL

    def __post_init__(self) -> None:
        for field in (
            "max_position_qty",
            "max_position_notional",
            "max_open_order_exposure",
            "max_daily_loss",
        ):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, _require_positive(value, field=f"RiskLimits.{field}"))

        if self.max_drawdown_pct is not None:
            pct = _require_positive(self.max_drawdown_pct, field="RiskLimits.max_drawdown_pct")
            if pct > 1.0:
                raise ValueError(f"RiskLimits.max_drawdown_pct must be in (0, 1], got {pct}")
            object.__setattr__(self, "max_drawdown_pct", pct)

        if self.max_leverage is not None:
            leverage = _require_positive(self.max_leverage, field="RiskLimits.max_leverage")
            if leverage < 1.0:
                raise ValueError(f"RiskLimits.max_leverage must be >= 1, got {leverage}")
            object.__setattr__(self, "max_leverage", leverage)

        if self.min_liquidation_distance_bps is not None:
            object.__setattr__(
                self,
                "min_liquidation_distance_bps",
                _require_positive(
                    self.min_liquidation_distance_bps, field="RiskLimits.min_liquidation_distance_bps"
                ),
            )

        if self.max_mark_age_ms is not None:
            age = self.max_mark_age_ms
            if isinstance(age, bool) or not isinstance(age, int) or age <= 0:
                raise ValueError(f"RiskLimits.max_mark_age_ms must be a positive int, got {age!r}")

        if not isinstance(self.kill_switch, bool):
            raise ValueError("RiskLimits.kill_switch must be a bool")
        if not isinstance(self.kill_switch_mode, KillSwitchMode):
            raise ValueError("RiskLimits.kill_switch_mode must be a KillSwitchMode")

    @property
    def effective_kill_switch_mode(self) -> KillSwitchMode:
        """兼容位 `kill_switch=True` 一律视为 HALT_ALL（P0001.5 行为不变）。"""
        return KillSwitchMode.HALT_ALL if self.kill_switch else self.kill_switch_mode

    @property
    def effective_leverage(self) -> float:
        """未配置 `max_leverage` 时按 1.0（不允许杠杆）。"""
        return 1.0 if self.max_leverage is None else self.max_leverage

    def enabled_checks(self) -> tuple[str, ...]:
        """已启用的限额名称（按 gate 的检查顺序）。"""
        checks: list[str] = []
        if self.effective_kill_switch_mode is not KillSwitchMode.NORMAL:
            checks.append("kill_switch")
        if self.max_mark_age_ms is not None:
            checks.append("mark_age")
        if self.max_position_qty is not None:
            checks.append("position_qty")
        if self.max_position_notional is not None:
            checks.append("position_notional")
        if self.max_open_order_exposure is not None:
            checks.append("open_order_exposure")
        # 不确定暴露与可用余额检查始终生效（未配置杠杆时按 1.0，即不允许杠杆）
        checks.append("uncertain_exposure")
        checks.append("available_balance")
        if self.max_leverage is not None:
            checks.append("leverage")
        if self.max_daily_loss is not None:
            checks.append("daily_loss")
        if self.max_drawdown_pct is not None:
            checks.append("drawdown")
        if self.min_liquidation_distance_bps is not None:
            checks.append("liquidation_distance")
        return tuple(checks)


__all__ = ["RiskLimits"]
