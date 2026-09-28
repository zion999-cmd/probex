"""历史风险 baseline 的**领域契约**（P0001.9.4.1 §8 / §10）。

这里只定义"外部历史事实如何进入 risk 视图"的数据契约，不含任何 Binance 细节（那在
`connectors/binance/private/income.py`）。三条纪律：

1. **daily PnL 与 drawdown/peak 是三件独立的事实**，不许合并成一个 `historical_known` 布尔
   （提案 §8 明确要求）；
2. **绝不为了 LIVE_READY 变绿而伪造 peak / drawdown**：本阶段 `drawdown_known` / `peak_equity_known`
   一律为 False（没有连续 equity 证据 ⇒ UNKNOWN）——**不得**用当前 equity 或 income 流水反推历史峰值；
3. **`daily_net_realized=None` 表示未知**（不是 0）：coverage 不完整 / 存在未分类记录 / 边界不自洽时保持未知。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from market.events.types import Milliseconds

#: baseline 来源标识：Binance income history（只读）。
BASELINE_SOURCE_BINANCE_INCOME = "BINANCE_INCOME"


class HistoricalRiskError(Exception):
    """历史风险 baseline 契约错误。"""


@dataclass(frozen=True, slots=True)
class HistoricalRiskBaseline:
    """当前 UTC 风险日、startup cutoff 之前的**账户级已实现现金流**事实。

    它与 `AccountingCore` 的本地账本是**互补**的两段（见 D-038 的水位线纪律）：

    ```text
    realized_pnl_today = baseline.daily_net_realized (income.time <= cutoff_ts)
                       + local_net_realized_since(cutoff_ts)   (本地 event_ts > cutoff_ts)
    ```
    """

    day_start_ts: Milliseconds
    cutoff_ts: Milliseconds
    #: 交易类现金流合计；`None` = 未知（coverage 不完整 / 有未分类记录 / 边界不自洽）
    daily_net_realized: float | None
    trading_rows: int = 0
    non_trading_rows: int = 0
    coverage_complete: bool = False
    source: str = BASELINE_SOURCE_BINANCE_INCOME
    detail: str = ""

    def __post_init__(self) -> None:
        for name in ("day_start_ts", "cutoff_ts"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise HistoricalRiskError(f"HistoricalRiskBaseline.{name} must be a non-negative int")
        if self.cutoff_ts < self.day_start_ts:
            raise HistoricalRiskError("HistoricalRiskBaseline.cutoff_ts must be >= day_start_ts")
        if self.daily_net_realized is not None:
            value = float(self.daily_net_realized)
            if not math.isfinite(value):
                raise HistoricalRiskError("HistoricalRiskBaseline.daily_net_realized must be finite")
            object.__setattr__(self, "daily_net_realized", value)
        for name in ("trading_rows", "non_trading_rows"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise HistoricalRiskError(f"HistoricalRiskBaseline.{name} must be a non-negative int")
        if not isinstance(self.coverage_complete, bool):
            raise HistoricalRiskError("HistoricalRiskBaseline.coverage_complete must be a bool")
        if not isinstance(self.source, str) or not self.source:
            raise HistoricalRiskError("HistoricalRiskBaseline.source must be a non-empty string")

    # ------------------------------------------------------------------ 已知性

    @property
    def daily_pnl_known(self) -> bool:
        """只有"覆盖完整 + 值存在"才算已知。"""
        return self.coverage_complete and self.daily_net_realized is not None

    @property
    def drawdown_known(self) -> bool:
        """本阶段永远是 False：没有连续 equity 证据就不可能有可信历史回撤（提案 §10）。"""
        return False

    @property
    def peak_equity_known(self) -> bool:
        return False

    def compose_daily_pnl(self, local_net_realized_since_cutoff: float | None) -> float | None:
        """把外部历史与本地账本拼成 `realized_pnl_today`；任一段未知 ⇒ 结果未知。"""
        if not self.daily_pnl_known:
            return None
        if local_net_realized_since_cutoff is None:
            return None
        return float(self.daily_net_realized) + float(local_net_realized_since_cutoff)


__all__ = [
    "BASELINE_SOURCE_BINANCE_INCOME",
    "HistoricalRiskBaseline",
    "HistoricalRiskError",
]
