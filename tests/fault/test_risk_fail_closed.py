"""Fault：Risk 必须 fail closed —— 数据缺失/异常时拒绝新增暴露（SC-9 / §13）。"""

from __future__ import annotations

import unittest
from dataclasses import replace

from portfolio.accounting import AccountingCore
from portfolio.types import LiquidationInfo, Side
from risk.gate import RiskGate
from risk.limits import RiskLimits
from risk.snapshot import build_risk_snapshot, utc_day_start_ms
from risk.types import OrderProposal, RiskDecisionType, RiskReasonCode
from tests.support import BASE_TS, SYMBOL, make_fill, make_funding


def _proposal(*, side: Side = Side.BUY, quantity: float = 0.1, reduce_only: bool = False) -> OrderProposal:
    return OrderProposal(symbol=SYMBOL, side=side, quantity=quantity, price=100.0, reduce_only=reduce_only)


def _core(*, position_qty: float = 1.0, balance: float = 10_000.0, mark: float | None = 100.0) -> AccountingCore:
    core = AccountingCore(initial_balance=balance)
    if position_qty > 0:
        core.record_fill(make_fill("f1", Side.BUY, 100.0, position_qty))
    elif position_qty < 0:
        core.record_fill(make_fill("f1", Side.SELL, 100.0, abs(position_qty)))
    if mark is not None:
        core.update_mark_price(SYMBOL, mark, timestamp=BASE_TS)
    return core


class FailClosedTest(unittest.TestCase):
    def test_missing_mark_blocks_increasing_orders(self) -> None:
        snapshot = build_risk_snapshot(_core(mark=None), symbol=SYMBOL, now_ms=BASE_TS)

        decision = RiskGate(RiskLimits(max_position_qty=10.0)).evaluate(_proposal(), snapshot)

        self.assertIs(decision.reason_code, RiskReasonCode.MISSING_MARK_PRICE)

    def test_unknown_daily_pnl_blocks_when_limit_configured(self) -> None:
        snapshot = build_risk_snapshot(_core(), symbol=SYMBOL, now_ms=BASE_TS, day_start_ts=None)

        decision = RiskGate(RiskLimits(max_daily_loss=100.0)).evaluate(_proposal(), snapshot)

        self.assertIs(decision.reason_code, RiskReasonCode.MISSING_DAILY_PNL)

    def test_unknown_liquidation_info_blocks_when_limit_configured(self) -> None:
        snapshot = build_risk_snapshot(_core(), symbol=SYMBOL, now_ms=BASE_TS)

        decision = RiskGate(RiskLimits(min_liquidation_distance_bps=50.0)).evaluate(_proposal(), snapshot)

        self.assertIs(decision.reason_code, RiskReasonCode.MISSING_LIQUIDATION_INFO)

    def test_unknown_drawdown_blocks_when_limit_configured(self) -> None:
        # 手工构造「equity 已知但 drawdown 未知」的快照（真实场景：startup baseline 之后历史峰值未知）
        snapshot = replace(build_risk_snapshot(_core(), symbol=SYMBOL, now_ms=BASE_TS), drawdown_pct=None)

        decision = RiskGate(RiskLimits(max_drawdown_pct=0.1)).evaluate(_proposal(), snapshot)

        self.assertTrue(decision.rejected)
        self.assertIs(decision.reason_code, RiskReasonCode.MISSING_DRAWDOWN)

    def test_negative_balance_blocks_new_exposure(self) -> None:
        core = _core(balance=1.0)
        core.record_funding(make_funding(-50.0, timestamp=BASE_TS + 1))
        snapshot = build_risk_snapshot(core, symbol=SYMBOL, now_ms=BASE_TS + 2)

        decision = RiskGate(RiskLimits(max_leverage=2.0)).evaluate(_proposal(), snapshot)

        self.assertIs(decision.decision, RiskDecisionType.REJECT)
        self.assertIs(decision.reason_code, RiskReasonCode.INSUFFICIENT_AVAILABLE_BALANCE)
        self.assertLess(snapshot.available_balance, 0.0)

    def test_flat_account_without_mark_price_fails_closed(self) -> None:
        # 空仓且没有任何 mark：无法评估名义价值 → 拒绝新增暴露
        core = AccountingCore(initial_balance=10_000.0)
        snapshot = build_risk_snapshot(core, symbol=SYMBOL, now_ms=BASE_TS)

        decision = RiskGate(RiskLimits(max_position_qty=1.0)).evaluate(_proposal(quantity=0.1), snapshot)

        self.assertIs(decision.reason_code, RiskReasonCode.MISSING_MARK_PRICE)

    def test_all_fail_closed_reasons_are_rejections(self) -> None:
        cases = (
            (RiskLimits(), _proposal(quantity=0.0)),
            (RiskLimits(), _proposal(quantity=-1.0)),
            (RiskLimits(kill_switch=True), _proposal()),
            (RiskLimits(max_daily_loss=1.0), _proposal()),
            (RiskLimits(min_liquidation_distance_bps=1.0), _proposal()),
        )
        for limits, proposal in cases:
            with self.subTest(reason=getattr(limits, "kill_switch", None), quantity=proposal.quantity):
                snapshot = build_risk_snapshot(_core(), symbol=SYMBOL, now_ms=BASE_TS)
                decision = RiskGate(limits).evaluate(proposal, snapshot)
                self.assertIs(decision.decision, RiskDecisionType.REJECT)
                self.assertIsNotNone(decision.reason_code)
                self.assertTrue(decision.details)

    def test_decision_is_not_a_bare_bool(self) -> None:
        snapshot = build_risk_snapshot(_core(), symbol=SYMBOL, now_ms=BASE_TS)
        decision = RiskGate(RiskLimits()).evaluate(_proposal(), snapshot)

        self.assertFalse(isinstance(decision, bool))
        self.assertIs(decision.decision, RiskDecisionType.ALLOW)
        self.assertIsNone(decision.reason_code)
        self.assertIn("exposure_class:increasing", decision.checks_applied)

    def test_rejected_decisions_record_the_checks_that_ran(self) -> None:
        snapshot = build_risk_snapshot(
            _core(), symbol=SYMBOL, now_ms=BASE_TS, day_start_ts=utc_day_start_ms(BASE_TS)
        )
        decision = RiskGate(RiskLimits(max_position_qty=1.0)).evaluate(
            _proposal(quantity=0.5), snapshot
        )

        self.assertIs(decision.reason_code, RiskReasonCode.POSITION_LIMIT)
        self.assertEqual(decision.checks_applied, ("order_parameters", "exposure_class:increasing", "position_qty"))

    def test_market_data_gate_has_priority_over_limits(self) -> None:
        snapshot = build_risk_snapshot(_core(), symbol=SYMBOL, now_ms=BASE_TS)
        decision = RiskGate(RiskLimits(max_position_qty=0.0001)).evaluate(_proposal(quantity=100.0), snapshot, book_healthy=False)

        self.assertIs(decision.reason_code, RiskReasonCode.BOOK_UNHEALTHY)




class BaselineDrawdownFailClosedTest(unittest.TestCase):
    """P0001.9.4 SC-1 / SC-2 / SC-3：startup baseline 之后历史峰值未知 ⇒ 不得伪造成 drawdown = 0。"""

    def _bootstrapped(self) -> AccountingCore:
        from portfolio.types import ExternalAccountBaseline

        core = AccountingCore(initial_balance=0.0)
        core.bootstrap_from_baseline(
            ExternalAccountBaseline(
                symbol=SYMBOL,
                wallet_balance=5_000.0,
                available_balance=4_900.0,
                position_qty=1.0,
                entry_price=100.0,
                mark_price=100.0,
                liquidation_price=50.0,
                captured_at=BASE_TS,
            )
        )
        return core

    def test_sc2_unknown_peak_is_not_replaced_by_current_equity(self) -> None:
        core = self._bootstrapped()
        core.update_mark_price(SYMBOL, 110.0, timestamp=BASE_TS + 1)  # equity 变化，但历史峰值仍未知

        snapshot = build_risk_snapshot(core, symbol=SYMBOL, now_ms=BASE_TS + 2)

        self.assertIsNone(snapshot.peak_equity)
        self.assertIsNone(snapshot.drawdown)
        self.assertIsNone(snapshot.drawdown_pct)

    def test_sc3_configured_drawdown_limit_fails_closed(self) -> None:
        core = self._bootstrapped()
        core.update_mark_price(SYMBOL, 110.0, timestamp=BASE_TS + 1)
        snapshot = build_risk_snapshot(core, symbol=SYMBOL, now_ms=BASE_TS + 2)

        decision = RiskGate(RiskLimits(max_drawdown_pct=0.2, max_position_qty=10.0)).evaluate(
            _proposal(), snapshot
        )

        self.assertIs(decision.decision, RiskDecisionType.REJECT)
        self.assertIs(decision.reason_code, RiskReasonCode.MISSING_DRAWDOWN)

    def test_reduce_only_still_allowed_with_unknown_drawdown(self) -> None:
        core = self._bootstrapped()
        snapshot = build_risk_snapshot(core, symbol=SYMBOL, now_ms=BASE_TS + 2)

        decision = RiskGate(RiskLimits(max_drawdown_pct=0.2, max_position_qty=10.0)).evaluate(
            _proposal(side=Side.SELL, quantity=0.5, reduce_only=True), snapshot
        )

        self.assertIs(decision.decision, RiskDecisionType.ALLOW)


if __name__ == "__main__":
    unittest.main()
