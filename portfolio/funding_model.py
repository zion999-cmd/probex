"""资金费结算模型（Binance USDⓈ-M 永续；模拟/回测路径，人类 2026-10-24 授权）。

venue 结算语义（事实，不是本项目发明）：
- 资金费每 8 小时在固定 UTC 边界结算（00:00 / 08:00 / 16:00）；
- 费率由 venue 随 mark price 发布（`markPriceUpdate.r`），并给出下次结算时间 `T`；
- 支付额 = 持仓名义 × 费率；`r > 0` 时**多头支付、空头收取**。

本模块只做**确定性结算判定**（无 wall-clock、无网络、无猜测费率）：

    update(rate, next_funding_time) 记录 venue 最新公布的 (费率, 结算点)；
    settle_due(now, position_qty, mark_price) 在时间越过结算点时产出
    唯一的 `FundingPayment`（同一结算点只结算一次）。

- 持仓为 0（flat）⇒ 结算点标记为已结算但不产生支付记录（金额 0 不入账）。
- 结算价用持仓的当前正式 mark（与风险/会计同源），不用 last trade。
- 没有发布过费率 ⇒ 不结算（UNKNOWN 不伪造 0 费率）。
"""

from __future__ import annotations

from dataclasses import dataclass

from market.events.types import Milliseconds
from portfolio.types import FundingPayment

#: Binance USDⓈ-M 永续的固定结算周期（毫秒）
FUNDING_INTERVAL_MS: Milliseconds = 8 * 60 * 60 * 1_000


class FundingModelError(ValueError):
    """资金费模型契约错误。"""


@dataclass(slots=True)
class FundingSettlementTracker:
    """单个 symbol 的待结算资金费状态（可变 Owner：回测/模拟步进器）。"""

    asset: str
    symbol: str
    _rate: float | None = None
    _boundary: Milliseconds | None = None
    _last_settled_boundary: Milliseconds | None = None

    def update(self, *, funding_rate: float | None,
               next_funding_time: Milliseconds | None) -> None:
        """登记 venue 最新公布的费率与结算点（只接受完整一对）。"""
        if funding_rate is None or next_funding_time is None:
            return
        if not isinstance(funding_rate, (int, float)) or isinstance(funding_rate, bool):
            raise FundingModelError("funding_rate must be a number")
        rate = float(funding_rate)
        if not -1.0 < rate < 1.0:
            raise FundingModelError(f"funding_rate must be within (-1, 1), got {rate!r}")
        if not isinstance(next_funding_time, int) or isinstance(next_funding_time, bool) \
                or next_funding_time <= 0:
            raise FundingModelError("next_funding_time must be a positive int epoch-ms")
        # 只接受更新的结算点（乱序/旧发布不覆盖）
        if self._boundary is not None and next_funding_time < self._boundary:
            return
        self._rate = rate
        self._boundary = int(next_funding_time)

    def settle_due(self, *, now_ms: Milliseconds, position_qty: float,
                    mark_price: float | None) -> FundingPayment | None:
        """若已越过待结算点，返回该周期唯一支付（flat 或 mark 未知 ⇒ 只标记不产出）。"""
        boundary = self._boundary
        if boundary is None or now_ms < boundary:
            return None
        if self._last_settled_boundary is not None and boundary <= self._last_settled_boundary:
            self._boundary = None
            return None
        self._last_settled_boundary = boundary
        self._boundary = None
        rate = self._rate
        # flat：无支付；mark 未知（理论上发布费率时 mark 必在）：不伪造
        if position_qty == 0.0 or mark_price is None or rate is None:
            return None
        if mark_price <= 0.0:
            raise FundingModelError(f"mark_price must be positive, got {mark_price!r}")
        amount = -float(position_qty) * float(mark_price) * rate
        return FundingPayment(symbol=self.symbol, amount=amount, asset=self.asset,
                                timestamp=boundary, rate=rate)

    @property
    def pending_boundary(self) -> Milliseconds | None:
        return self._boundary

    @property
    def last_settled_boundary(self) -> Milliseconds | None:
        return self._last_settled_boundary


__all__ = ["FUNDING_INTERVAL_MS", "FundingModelError", "FundingSettlementTracker"]
