"""P0017.2：组合 gross-notional 预算（组合第二道闸，纯函数）。

职责边界
--------
- 只做**只读计算 + 判定**：给定一个一致视图（各 symbol 持仓名义、在途名义、候选单），
  判断候选单加入后组合 projected gross 是否超限 / 是否无法建立安全上界。
- **不**提交订单、**不**改会计/tracker、**不**复制 `RiskGate` 的单标的检查。
- 口径（D1/D3/D6）：
  - gross（Σ绝对值，多空不抵消），USDT 名义；不是保证金、不是损失预算。
  - committed = Σ_s position_notional_s + Σ_s pending_notional_s（持仓与在途互斥，见下）。
  - 候选单只在它**增加风险**时占用组合预算；真正的 reduce-only 不增加 projected gross。

互斥与在途口径（D6，依赖现有状态机 `execution/types.py`）
--------------------------------------------------------
- 持仓名义来自 `AccountingCore`（已成交）；在途名义来自 `OrderTracker.exposure_by_symbol()`，
  其 `Order.notional = remaining_quantity * price`——`remaining = quantity - filled_quantity`，
  故已成交部分只进持仓、未成交部分只进在途，**同一数量不重复**。
- 在途集合：ACTIVE（PENDING_CREATE/OPEN/PARTIALLY_FILLED/PENDING_CANCEL，撤单确认前不释放）
  + LOST（不确定，保守保留）；终态（FILLED/CANCELED/FAILED/EXPIRED）不贡献。
- 成交/记账在 `ExecutionEngine.on_events` 同一同步临界区完成（tracker 先、accounting 后），
  下一次决策看到的是一致视图（见 P0017.2 设计 §6.1 核验）。
"""

from __future__ import annotations

from dataclasses import dataclass

from risk.limits import RiskLimits
from risk.types import OrderProposal, RiskDecision, RiskDecisionType, RiskReasonCode


@dataclass(frozen=True, slots=True)
class PortfolioCommitment:
    """组合在某一一致视图下的名义承诺（计价币，如 USDT）。"""

    #: 各 symbol 持仓名义 |qty·mark|
    position_by_symbol: dict[str, float]
    #: 各 symbol 在途名义（confirmed + uncertain；来自 tracker 同口径）
    pending_by_symbol: dict[str, float]
    #: 各 symbol 是否存在不可量化的不确定订单（True ⇒ 无法建立安全上界）
    has_unresolved_by_symbol: dict[str, bool]

    @property
    def position_gross(self) -> float:
        return float(sum(self.position_by_symbol.values()))

    @property
    def pending_gross(self) -> float:
        return float(sum(self.pending_by_symbol.values()))

    @property
    def committed_gross(self) -> float:
        return self.position_gross + self.pending_gross

    @property
    def has_unresolved(self) -> bool:
        return any(self.has_unresolved_by_symbol.values())

    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.position_by_symbol) | set(self.pending_by_symbol)
                            | set(self.has_unresolved_by_symbol)))


@dataclass(frozen=True, slots=True)
class PortfolioProjection:
    """候选单加入后的组合 projected gross 结果。"""

    committed_gross: float
    #: 候选单对组合 gross 的**最坏情况增量**（reduce-only 为 0；其余为订单名义）
    candidate_increment: float
    projected_gross: float
    #: 候选单是否可被证明只降低风险（真正 reduce-only）
    candidate_reduces: bool
    unknown: bool = False
    unknown_reason: str = ""

    @property
    def is_safe_bound(self) -> bool:
        return not self.unknown


def _candidate_increment(proposal: OrderProposal, commitment: PortfolioCommitment) -> tuple[float, bool]:
    """计算候选单对组合 gross 的最坏情况增量。

    - 减少敞口且不反手（reduce_only 且 |qty| <= |当前净持仓|，方向相反）：增量 0。
    - 其他（开仓、反手、超量 reduce-only、非 reduce_only 的反向单）：按完整订单名义计（保守）。
    用在途+持仓的净敞口方向判断"是否会反手"，避免错误放行反手单。
    """
    notional = proposal.notional
    symbol = proposal.symbol
    signed_qty = proposal.signed_quantity
    if signed_qty == 0.0:
        return 0.0, True
    # 该 symbol 当前净持仓（数量口径无法在纯名义层得到符号，改由调用方在 build 时提供净数量）。
    # 这里的保守默认：无法证明降险 ⇒ 全额计入（由 evaluate_portfolio_budget 的 reduce 判定细化）。
    return notional, False


def project_portfolio_gross(
    proposal: OrderProposal,
    commitment: PortfolioCommitment,
    *,
    net_position_qty_by_symbol: dict[str, float] | None = None,
) -> PortfolioProjection:
    """计算候选单加入后的 projected gross（纯函数；不判限额）。

    `net_position_qty_by_symbol`：各 symbol 净持仓数量（带符号），用于可靠判定 reduce-only/反手。
    缺该符号的净数量时：reduce-only 也无法证明安全 ⇒ 保守按全额计（不静默放行）。
    """
    net_qty = (net_position_qty_by_symbol or {}).get(proposal.symbol)
    increment = proposal.notional
    reduces = False

    if proposal.reduce_only and net_qty is not None and net_qty != 0.0:
        same_direction = (net_qty > 0) == (proposal.signed_quantity > 0)
        closes_only = abs(proposal.signed_quantity) <= abs(net_qty)
        if not same_direction and closes_only:
            # 可证明：方向相反且不超过现有持仓 ⇒ 最坏情况下也只降低该 symbol 敞口
            increment = 0.0
            reduces = True

    projected = commitment.committed_gross + increment
    return PortfolioProjection(
        committed_gross=commitment.committed_gross,
        candidate_increment=increment,
        projected_gross=projected,
        candidate_reduces=reduces)


def evaluate_portfolio_budget(
    proposal: OrderProposal,
    commitment: PortfolioCommitment,
    limits: RiskLimits,
    *,
    net_position_qty_by_symbol: dict[str, float] | None = None,
    applied: list[str] | None = None,
) -> RiskDecision | None:
    """组合第二道闸。

    - 未配置 `max_portfolio_gross_notional` ⇒ 返回 None（不启用，单标的零回归）。
    - 存在不可量化订单（unresolved）⇒ PORTFOLIO_EXPOSURE_UNKNOWN（fail-closed）。
    - 可证明只降险的 reduce-only ⇒ 放行组合闸（不豁免单标的 RiskGate 的其他检查）。
    - projected gross **严格大于**限额 ⇒ PORTFOLIO_NOTIONAL_LIMIT；等于限额允许（D：≤）。
    """
    if limits.max_portfolio_gross_notional is None:
        return None
    if applied is not None:
        applied.append("portfolio_gross_notional")

    if commitment.has_unresolved:
        return RiskDecision(
            decision=RiskDecisionType.REJECT,
            reason_code=RiskReasonCode.PORTFOLIO_EXPOSURE_UNKNOWN,
            details="portfolio exposure has unquantifiable orders; a safe gross upper bound "
                    "cannot be established (fail closed)")

    projection = project_portfolio_gross(
        proposal, commitment, net_position_qty_by_symbol=net_position_qty_by_symbol)
    if projection.candidate_reduces:
        return None  # 组合层放行；单标的 RiskGate 仍照常评估

    limit_value = float(limits.max_portfolio_gross_notional)
    if projection.projected_gross > limit_value + 1e-9:
        return RiskDecision(
            decision=RiskDecisionType.REJECT,
            reason_code=RiskReasonCode.PORTFOLIO_NOTIONAL_LIMIT,
            details=(f"projected portfolio gross {projection.projected_gross:.6f} "
                     f"(committed={projection.committed_gross:.6f}, "
                     f"candidate_increment={projection.candidate_increment:.6f}) "
                     f"exceeds max_portfolio_gross_notional={limit_value}"))
    return None


__all__ = [
    "PortfolioCommitment",
    "PortfolioProjection",
    "evaluate_portfolio_budget",
    "project_portfolio_gross",
]
