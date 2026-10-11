"""P0017.2 Batch 3：组合 gross-notional 第二道闸（纯函数；含 6000+6000>10000）。"""

from __future__ import annotations

import unittest

from market.events.types import Milliseconds
from portfolio.types import Side
from risk.limits import RiskLimits
from risk.portfolio_budget import (
    PortfolioCommitment,
    evaluate_portfolio_budget,
    project_portfolio_gross,
)
from risk.types import (
    KillSwitchMode,
    OrderProposal,
    RiskReasonCode,
)


def proposal(symbol: str, side: Side, quantity: float, price: float, *,
             reduce_only: bool = False) -> OrderProposal:
    return OrderProposal(symbol=symbol, side=side, quantity=quantity, price=price,
                          reduce_only=reduce_only)


def commitment(*, positions=None, pending=None, unresolved=None) -> PortfolioCommitment:
    return PortfolioCommitment(
        position_by_symbol=dict(positions or {}),
        pending_by_symbol=dict(pending or {}),
        has_unresolved_by_symbol=dict(unresolved or {}))


LIMIT = RiskLimits(max_portfolio_gross_notional=10_000.0,
                    kill_switch_mode=KillSwitchMode.NORMAL)


class PortfolioBudgetTest(unittest.TestCase):
    def test_disabled_when_limit_none(self) -> None:
        self.assertIsNone(evaluate_portfolio_budget(
            proposal("BTCUSDT", Side.BUY, 100, 100), commitment(),
            RiskLimits(kill_switch_mode=KillSwitchMode.NORMAL)))

    def test_two_symbols_each_ok_but_portfolio_over_limit_rejected(self) -> None:
        # 已有 6000 BTC + 0 ETH；再来 ETH 6000 ⇒ 12000 > 10000
        comm = commitment(positions={"BTCUSDT": 6_000.0})
        decision = evaluate_portfolio_budget(
            proposal("ETHUSDT", Side.BUY, 60, 100), comm, LIMIT)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.reason_code, RiskReasonCode.PORTFOLIO_NOTIONAL_LIMIT)

    def test_each_under_and_portfolio_under_allowed(self) -> None:
        comm = commitment(positions={"BTCUSDT": 4_000.0})
        decision = evaluate_portfolio_budget(
            proposal("ETHUSDT", Side.BUY, 30, 100), comm, LIMIT)  # +3000 = 7000
        self.assertIsNone(decision)

    def test_equal_to_limit_is_allowed(self) -> None:
        comm = commitment(positions={"BTCUSDT": 4_000.0})
        decision = evaluate_portfolio_budget(
            proposal("ETHUSDT", Side.BUY, 60, 100), comm, LIMIT)  # 4000+6000=10000
        self.assertIsNone(decision)

    def test_gross_does_not_net_longs_and_shorts(self) -> None:
        # +6000 多 与 4000 空：gross=10000，再来 1 名义都应超限
        comm = commitment(positions={"BTCUSDT": 6_000.0, "ETHUSDT": 4_000.0})
        decision = evaluate_portfolio_budget(
            proposal("SOLUSDT", Side.BUY, 1, 1), comm, LIMIT)
        self.assertEqual(decision.reason_code, RiskReasonCode.PORTFOLIO_NOTIONAL_LIMIT)

    def test_pending_orders_count_toward_committed(self) -> None:
        comm = commitment(positions={"BTCUSDT": 3_000.0},
                           pending={"ETHUSDT": 3_000.0})  # committed=6000
        decision = evaluate_portfolio_budget(
            proposal("SOLUSDT", Side.BUY, 41, 100), comm, LIMIT)  # +4100 = 10100
        self.assertEqual(decision.reason_code, RiskReasonCode.PORTFOLIO_NOTIONAL_LIMIT)

    def test_unresolved_orders_fail_closed(self) -> None:
        comm = commitment(positions={"BTCUSDT": 1_000.0},
                           unresolved={"ETHUSDT": True})
        decision = evaluate_portfolio_budget(
            proposal("ETHUSDT", Side.BUY, 1, 100), comm, LIMIT)
        self.assertEqual(decision.reason_code, RiskReasonCode.PORTFOLIO_EXPOSURE_UNKNOWN)

    def test_proven_reduce_only_adds_zero(self) -> None:
        # 持有多 6000；反向卖不超过持仓 ⇒ 增量 0，不被组合闸拦
        comm = commitment(positions={"BTCUSDT": 6_000.0})
        decision = evaluate_portfolio_budget(
            proposal("BTCUSDT", Side.SELL, 10, 100, reduce_only=True), comm, LIMIT,
            net_position_qty_by_symbol={"BTCUSDT": 60.0})
        self.assertIsNone(decision)
        projection = project_portfolio_gross(
            proposal("BTCUSDT", Side.SELL, 10, 100, reduce_only=True), comm,
            net_position_qty_by_symbol={"BTCUSDT": 60.0})
        self.assertTrue(projection.candidate_reduces)
        self.assertEqual(projection.candidate_increment, 0.0)

    def test_oversized_reduce_only_counts_full_notional(self) -> None:
        # 持仓 1（名义100），reduce-only 卖 60（名义6000）会反手 ⇒ 保守全额计入
        comm = commitment(positions={"BTCUSDT": 100.0})
        decision = evaluate_portfolio_budget(
            proposal("BTCUSDT", Side.SELL, 60, 100, reduce_only=True), comm, LIMIT,
            net_position_qty_by_symbol={"BTCUSDT": 1.0})
        # committed 100 + 6000 = 6100 < 10000 ⇒ 允许，但增量必须是 6000（不是 0）
        projection = project_portfolio_gross(
            proposal("BTCUSDT", Side.SELL, 60, 100, reduce_only=True), comm,
            net_position_qty_by_symbol={"BTCUSDT": 1.0})
        self.assertAlmostEqual(projection.candidate_increment, 6_000.0)
        self.assertFalse(projection.candidate_reduces)
        self.assertIsNone(decision)

    def test_reduce_only_without_net_qty_is_conservative(self) -> None:
        # 无净持仓数量证据 ⇒ 不能证明降险 ⇒ 全额计入
        comm = commitment(positions={"BTCUSDT": 0.0})
        projection = project_portfolio_gross(
            proposal("BTCUSDT", Side.SELL, 5, 100, reduce_only=True), comm,
            net_position_qty_by_symbol=None)
        self.assertAlmostEqual(projection.candidate_increment, 500.0)


if __name__ == "__main__":
    unittest.main()
